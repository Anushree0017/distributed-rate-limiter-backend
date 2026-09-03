-- Key format: rl:sliding_window_log:{scope}:{identifier_type}:{identifier_value}
--   -> ZSET { member: request_id (opaque, Python-generated), score: timestamp_ms }
-- KEYS[1] = the key above
-- ARGV[1] = window_size_ms, ARGV[2] = max_requests
-- ARGV[3] = request_id — a unique value generated in Python (uuid4), since
--   two requests can land in the same millisecond and a ZSET member must be
--   unique; Lua must not generate this itself (no math.random(), guidelines §4).
-- Returns: { allowed(1/0), limit, remaining, retry_after_ms(-1 if allowed), reset_at_ms }
local window_size_ms = tonumber(ARGV[1])
local max_requests = tonumber(ARGV[2])
local request_id = ARGV[3]

local time = redis.call('TIME')
local now_ms = math.floor(tonumber(time[1]) * 1000 + tonumber(time[2]) / 1000)

local key = KEYS[1]
local window_start_ms = now_ms - window_size_ms

-- Bounds ZSET growth — without this, a hot key grows unboundedly and every
-- later ZCARD/ZADD gets slower (guidelines §9).
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start_ms)

local count = redis.call('ZCARD', key)

local function oldest_reset_at_ms()
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if #oldest > 0 then
        return tonumber(oldest[2]) + window_size_ms
    end
    return now_ms + window_size_ms
end

if count < max_requests then
    redis.call('ZADD', key, now_ms, request_id)
    redis.call('PEXPIRE', key, window_size_ms)
    local remaining = max_requests - count - 1
    return {1, max_requests, remaining, -1, oldest_reset_at_ms()}
end

local reset_at_ms = oldest_reset_at_ms()
local retry_after_ms = reset_at_ms - now_ms
if retry_after_ms < 0 then retry_after_ms = 0 end
redis.call('PEXPIRE', key, window_size_ms)
return {0, max_requests, 0, retry_after_ms, reset_at_ms}
