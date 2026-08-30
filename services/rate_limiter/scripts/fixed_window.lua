-- Key format: rl:fixed_window:{scope}:{identifier_type}:{identifier_value}
--   -> HASH { count: int, window_start_ms: int }
-- KEYS[1] = the key above
-- ARGV[1] = window_size_ms, ARGV[2] = max_requests
-- Returns: { allowed(1/0), limit, remaining, retry_after_ms(-1 if allowed), reset_at_ms }
local window_size_ms = tonumber(ARGV[1])
local max_requests = tonumber(ARGV[2])

local time = redis.call('TIME')
local now_ms = math.floor(tonumber(time[1]) * 1000 + tonumber(time[2]) / 1000)

local key = KEYS[1]
local data = redis.call('HMGET', key, 'count', 'window_start_ms')
local count = tonumber(data[1])
local window_start_ms = tonumber(data[2])

if window_start_ms == nil or (now_ms - window_start_ms) >= window_size_ms then
    count = 0
    window_start_ms = now_ms
end

local reset_at_ms = window_start_ms + window_size_ms

if count < max_requests then
    count = count + 1
    redis.call('HSET', key, 'count', count, 'window_start_ms', window_start_ms)
    -- NX: only arm the TTL on a fresh window, so a mid-window increment never
    -- pushes the expiry out further than the fixed window it belongs to.
    redis.call('PEXPIRE', key, window_size_ms, 'NX')
    return {1, max_requests, max_requests - count, -1, reset_at_ms}
end

redis.call('HSET', key, 'count', count, 'window_start_ms', window_start_ms)
redis.call('PEXPIRE', key, window_size_ms, 'NX')
local retry_after_ms = reset_at_ms - now_ms
if retry_after_ms < 0 then retry_after_ms = 0 end
return {0, max_requests, 0, retry_after_ms, reset_at_ms}
