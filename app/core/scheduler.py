import asyncio
import time
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("orbiter.scheduler")

class TokenBudgetManager:
    """
    Tracks and limits cumulative token usage (input + output) per agent session.
    """
    def __init__(self, default_ceiling: int = 50000):
        self.default_ceiling = default_ceiling
        self.usage: Dict[str, int] = {}
        self.lock = asyncio.Lock()

    async def record_tokens(self, session_id: str, tokens: int):
        async with self.lock:
            self.usage[session_id] = self.usage.get(session_id, 0) + tokens
            logger.info(f"Session {session_id} consumed {tokens} tokens. Total: {self.usage[session_id]}")

    async def get_usage(self, session_id: str) -> int:
        async with self.lock:
            return self.usage.get(session_id, 0)

    async def check_budget(self, session_id: str, ceiling: Optional[int] = None) -> bool:
        limit = ceiling if ceiling is not None else self.default_ceiling
        async with self.lock:
            current = self.usage.get(session_id, 0)
            return current < limit


class CircuitBreaker:
    """
    State machine: CLOSED, OPEN, HALF_OPEN.
    Fast-fails queries when the target API has excessive error rates.
    """
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold: float = 0.5, cooldown_period: float = 15.0, window_size: int = 5):
        self.failure_threshold = failure_threshold
        self.cooldown_period = cooldown_period
        self.window_size = window_size
        
        self.state = self.CLOSED
        self.history: List[bool] = []  # True = success, False = failure
        self.last_state_change = time.time()
        self.lock = asyncio.Lock()

    async def can_execute(self) -> bool:
        async with self.lock:
            if self.state == self.CLOSED:
                return True
            
            if self.state == self.OPEN:
                # Check if cooldown has elapsed
                if time.time() - self.last_state_change > self.cooldown_period:
                    self.state = self.HALF_OPEN
                    self.last_state_change = time.time()
                    logger.warning("CircuitBreaker transitioning to HALF_OPEN (cooldown elapsed)")
                    return True
                return False
            
            if self.state == self.HALF_OPEN:
                # Only allow one request to trial. 
                # For simplicity, we just allow it; caller will report result immediately.
                return True
            
            return False

    async def report_success(self):
        async with self.lock:
            self.history.append(True)
            if len(self.history) > self.window_size:
                self.history.pop(0)

            if self.state == self.HALF_OPEN:
                self.state = self.CLOSED
                self.history.clear()
                self.last_state_change = time.time()
                logger.info("CircuitBreaker returned to CLOSED state after successful trial")

    async def report_failure(self):
        async with self.lock:
            self.history.append(False)
            if len(self.history) > self.window_size:
                self.history.pop(0)

            # Check error rate in the window
            failures = self.history.count(False)
            total = len(self.history)
            error_rate = failures / total if total > 0 else 0.0

            if self.state in (self.CLOSED, self.HALF_OPEN):
                if error_rate >= self.failure_threshold or self.state == self.HALF_OPEN:
                    self.state = self.OPEN
                    self.last_state_change = time.time()
                    logger.error(f"CircuitBreaker tripped to OPEN. Error rate: {error_rate:.2f}")


class AIMDController:
    """
    TCP-like Additive Increase / Multiplicative Decrease controller.
    Dynamically adjusts available concurrency based on request latency and failures.
    """
    def __init__(self, min_limit: int = 1, max_limit: int = 10, initial_limit: int = 4,
                 alpha: int = 1, beta: float = 0.5, target_latency: float = 5.0):
        self.min_limit = min_limit
        self.max_limit = max_limit
        self.limit = initial_limit
        self.alpha = alpha
        self.beta = beta
        self.target_latency = target_latency
        self.lock = asyncio.Lock()

    async def report_success(self, latency: float) -> int:
        async with self.lock:
            old_limit = self.limit
            if latency <= self.target_latency:
                # Additive Increase
                self.limit = min(self.max_limit, self.limit + self.alpha)
                if self.limit != old_limit:
                    logger.info(f"AIMD Concurrency Increased: {old_limit} -> {self.limit} (latency: {latency:.2f}s)")
            else:
                # Latency backpressure (treat as slow request -> Multiplicative Decrease)
                self.limit = max(self.min_limit, int(self.limit * self.beta))
                if self.limit != old_limit:
                    logger.warning(f"AIMD Concurrency Decreased (slow request): {old_limit} -> {self.limit} (latency: {latency:.2f}s)")
            return self.limit

    async def report_failure(self) -> int:
        async with self.lock:
            old_limit = self.limit
            # Multiplicative Decrease on failure
            self.limit = max(self.min_limit, int(self.limit * self.beta))
            if self.limit != old_limit:
                logger.error(f"AIMD Concurrency Decreased (failure): {old_limit} -> {self.limit}")
            return self.limit

    async def get_limit(self) -> int:
        async with self.lock:
            return self.limit


class AdmissionControl:
    """
    Orchestrates the dynamic Semaphore based on AIMD controller limits,
    handling request slot admission.
    """
    def __init__(self, aimd: AIMDController):
        self.aimd = aimd
        self.current_limit = aimd.limit
        self.semaphore = asyncio.Semaphore(self.current_limit)
        self.lock = asyncio.Lock()

    async def acquire(self):
        # Dynamically adjust semaphore capacity if AIMD limit changed
        async with self.lock:
            target_limit = await self.aimd.get_limit()
            if target_limit != self.current_limit:
                diff = target_limit - self.current_limit
                self.current_limit = target_limit
                if diff > 0:
                    # Increase capacity: release the semaphore diff times
                    for _ in range(diff):
                        self.semaphore.release()
                elif diff < 0:
                    # Decrease capacity: acquire the semaphore diff times
                    try:
                        self.semaphore._value = max(0, self.semaphore._value + diff)
                    except AttributeError:
                        pass
        await self.semaphore.acquire()

    def release(self):
        self.semaphore.release()


class RateLimitTracker:
    """
    Tracks API rate limits in a rolling window.
    Supports Request Per Minute (RPM) and Tokens Per Minute (TPM) safety bounds.
    """
    def __init__(self, max_rpm: int = 50, max_tpm: int = 40000):
        self.max_rpm = max_rpm
        self.max_tpm = max_tpm
        self.requests: List[float] = []  # Timestamps of requests
        self.token_history: List[tuple] = []  # List of (timestamp, token_count)
        self.lock = asyncio.Lock()

    async def check_limit_and_delay(self, estimated_tokens: int = 1000) -> float:
        """
        Calculates if we are close to rate limits and returns the delay (in seconds) required.
        """
        async with self.lock:
            now = time.time()
            one_minute_ago = now - 60.0

            # Prune old logs
            self.requests = [t for t in self.requests if t > one_minute_ago]
            self.token_history = [(t, count) for t, count in self.token_history if t > one_minute_ago]

            current_rpm = len(self.requests)
            current_tpm = sum(count for _, count in self.token_history)

            delay = 0.0

            # RPM limit backoff
            if current_rpm >= self.max_rpm:
                if self.requests:
                    delay = max(delay, (self.requests[0] + 60.0) - now)

            # TPM limit backoff
            if current_tpm + estimated_tokens >= self.max_tpm:
                accumulated = 0
                for t, count in self.token_history:
                    accumulated += count
                    if current_tpm - accumulated + estimated_tokens < self.max_tpm:
                        delay = max(delay, (t + 60.0) - now)
                        break

            return delay

    async def record_request(self, tokens: int = 0):
        async with self.lock:
            now = time.time()
            self.requests.append(now)
            self.token_history.append((now, tokens))


class HiveMindScheduler:
    """
    Combines the five HiveMind scheduler primitives.
    """
    def __init__(self, max_rpm: int = 50, max_tpm: int = 40000, default_ceiling: int = 50000):
        self.aimd = AIMDController()
        self.admission = AdmissionControl(self.aimd)
        self.rate_tracker = RateLimitTracker(max_rpm=max_rpm, max_tpm=max_tpm)
        self.circuit_breaker = CircuitBreaker()
        self.token_budget = TokenBudgetManager(default_ceiling=default_ceiling)

    async def enter_turn(self, session_id: str, estimated_tokens: int = 1000) -> float:
        """
        Acquires slots and handles rate limits/circuit breakers.
        Returns the timestamp when entering execution.
        """
        # 1. Check Circuit Breaker
        if not await self.circuit_breaker.can_execute():
            raise RuntimeError("Circuit breaker is OPEN. API calls are fast-failing.")

        # 2. Check Token Budget
        if not await self.token_budget.check_budget(session_id):
            raise ValueError(f"Session {session_id} has exceeded its token budget limit.")

        # 3. Rate limit delay
        delay = await self.rate_tracker.check_limit_and_delay(estimated_tokens)
        if delay > 0:
            logger.warning(f"RateLimitTracker backoff triggered. Waiting for {delay:.2f} seconds.")
            await asyncio.sleep(delay)

        # 4. Concurrency Admission
        await self.admission.acquire()
        await self.rate_tracker.record_request(estimated_tokens)
        return time.time()

    async def exit_turn(self, session_id: str, start_time: float, success: bool, actual_tokens: int = 0):
        """
        Releases the concurrency slot, records actual tokens, and updates AIMD/Circuit Breaker.
        """
        try:
            # 1. Release concurrency slot
            self.admission.release()

            # 2. Record actual tokens
            await self.token_budget.record_tokens(session_id, actual_tokens)

            # 3. Update feedback loops
            if success:
                latency = time.time() - start_time
                await self.aimd.report_success(latency)
                await self.circuit_breaker.report_success()
            else:
                await self.aimd.report_failure()
                await self.circuit_breaker.report_failure()
        except Exception as e:
            logger.error(f"Error in exit_turn: {e}", exc_info=True)
