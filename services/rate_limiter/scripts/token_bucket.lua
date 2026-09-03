-- Key format: rl:token_bucket:{scope}:{identifier_type}:{identifier_value}
--   -> HASH { tokens: float, last_refill_ms: int }
-- KEYS[1] = the key above
-- ARGV[1] = capacity, ARGV[2] = refill_rate_per_second
-- Returns: { allowed(1/0), limit, remaining, retry_after_ms(-1 if allowed), reset_at_ms }
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])

local time = redis.call('TIME')
local now_ms = math.floor(tonumber(time[1]) * 1000 + tonumber(time[2]) / 1000)

local key = KEYS[1]
local data = redis.call('HMGET', key, 'tokens', 'last_refill_ms')
local tokens = tonumber(data[1])
local last_refill_ms = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill_ms = now_ms
end

local elapsed_ms = now_ms - last_refill_ms
if elapsed_ms > 0 then
    tokens = math.min(capacity, tokens + (elapsed_ms / 1000) * refill_rate)
end

-- TTL = time for an empty bucket to refill to full; a bucket idle that long
-- is equivalent to a freshly-created one, so it's safe to let the key expire.
local ttl_ms = math.max(1, math.ceil(((capacity - tokens) / refill_rate) * 1000) + 1000)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HSET', key, 'tokens', tokens, 'last_refill_ms', now_ms)
    redis.call('PEXPIRE', key, ttl_ms)
    local reset_at_ms = now_ms + math.ceil(((capacity - tokens) / refill_rate) * 1000)
    return {1, capacity, math.floor(tokens), -1, reset_at_ms}
end

redis.call('HSET', key, 'tokens', tokens, 'last_refill_ms', now_ms)
redis.call('PEXPIRE', key, ttl_ms)
local deficit = 1 - tokens
local retry_after_ms = math.ceil((deficit / refill_rate) * 1000)
return {0, capacity, 0, retry_after_ms, now_ms + retry_after_ms}
