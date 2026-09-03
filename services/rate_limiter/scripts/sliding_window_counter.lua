-- Key format: rl:sliding_window_counter:{scope}:{identifier_type}:{identifier_value}
--   -> HASH { window_start_ms: int, curr_count: int, prev_count: int }
-- KEYS[1] = the key above
-- ARGV[1] = window_size_ms, ARGV[2] = max_requests
-- Returns: { allowed(1/0), limit, remaining, retry_after_ms(-1 if allowed), reset_at_ms }
local window_size_ms = tonumber(ARGV[1])
local max_requests = tonumber(ARGV[2])

local time = redis.call('TIME')
local now_ms = math.floor(tonumber(time[1]) * 1000 + tonumber(time[2]) / 1000)

local key = KEYS[1]
local window_start_ms = math.floor(now_ms / window_size_ms) * window_size_ms

local data = redis.call('HMGET', key, 'window_start_ms', 'curr_count', 'prev_count')
local stored_window_start = tonumber(data[1])
local curr_count = tonumber(data[2]) or 0
local prev_count = tonumber(data[3]) or 0

if stored_window_start == nil then
    stored_window_start = window_start_ms
elseif stored_window_start ~= window_start_ms then
    if window_start_ms - stored_window_start == window_size_ms then
        prev_count = curr_count
    else
        prev_count = 0
    end
    curr_count = 0
    stored_window_start = window_start_ms
end

local elapsed_in_window_ms = now_ms - window_start_ms
local weight = 1 - (elapsed_in_window_ms / window_size_ms)
local estimated_count = prev_count * weight + curr_count
local reset_at_ms = window_start_ms + window_size_ms

if estimated_count < max_requests then
    curr_count = curr_count + 1
    redis.call('HSET', key, 'window_start_ms', stored_window_start, 'curr_count', curr_count, 'prev_count', prev_count)
    -- Retain two full windows so the previous window's count survives to be
    -- read as `prev_count` by the next window's requests.
    redis.call('PEXPIRE', key, window_size_ms * 2)
    local remaining = math.floor(max_requests - estimated_count - 1)
    if remaining < 0 then remaining = 0 end
    return {1, max_requests, remaining, -1, reset_at_ms}
end

redis.call('HSET', key, 'window_start_ms', stored_window_start, 'curr_count', curr_count, 'prev_count', prev_count)
redis.call('PEXPIRE', key, window_size_ms * 2)
local retry_after_ms = window_size_ms - elapsed_in_window_ms
if retry_after_ms < 0 then retry_after_ms = 0 end
return {0, max_requests, 0, retry_after_ms, reset_at_ms}
