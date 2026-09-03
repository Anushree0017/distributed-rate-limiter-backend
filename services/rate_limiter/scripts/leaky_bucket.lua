-- Key format: rl:leaky_bucket:{scope}:{identifier_type}:{identifier_value}
--   -> HASH { level: float, last_leak_ms: int }
-- KEYS[1] = the key above
-- ARGV[1] = capacity, ARGV[2] = leak_rate_per_second
-- Returns: { allowed(1/0), limit, remaining, retry_after_ms(-1 if allowed), reset_at_ms }
local capacity = tonumber(ARGV[1])
local leak_rate = tonumber(ARGV[2])

local time = redis.call('TIME')
local now_ms = math.floor(tonumber(time[1]) * 1000 + tonumber(time[2]) / 1000)

local key = KEYS[1]
local data = redis.call('HMGET', key, 'level', 'last_leak_ms')
local level = tonumber(data[1])
local last_leak_ms = tonumber(data[2])

if level == nil then
    level = 0.0
    last_leak_ms = now_ms
end

local elapsed_ms = now_ms - last_leak_ms
if elapsed_ms > 0 then
    level = math.max(0.0, level - (elapsed_ms / 1000) * leak_rate)
end

-- TTL = time for a full bucket to leak down to empty; idle that long means
-- the key is equivalent to a fresh, empty bucket.
local ttl_ms = math.max(1, math.ceil((level / leak_rate) * 1000) + 1000)

-- Require a full unit of headroom, not just any (possibly epsilon-sized)
-- room — with a real clock, any nonzero elapsed time leaks a tiny fractional
-- amount, and `level < capacity` would treat that epsilon as enough to admit
-- a whole new unit.
if level <= capacity - 1 then
    level = level + 1
    redis.call('HSET', key, 'level', level, 'last_leak_ms', now_ms)
    redis.call('PEXPIRE', key, ttl_ms)
    local reset_at_ms = now_ms + math.ceil((level / leak_rate) * 1000)
    return {1, capacity, math.floor(capacity - level), -1, reset_at_ms}
end

redis.call('HSET', key, 'level', level, 'last_leak_ms', now_ms)
redis.call('PEXPIRE', key, ttl_ms)
local excess = level - capacity + 1
local retry_after_ms = math.ceil((excess / leak_rate) * 1000)
return {0, capacity, 0, retry_after_ms, now_ms + retry_after_ms}
