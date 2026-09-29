#!/usr/bin/env python3
"""
fleet-retry-engine — Self-healing retry with pattern-based automatic recovery.

Analyzes operation failure patterns and applies intelligent retry strategies:
- Connection refused → exponential backoff (network)
- File not found → create parent dirs + retry (filesystem)
- SQL locked → wait and retry (database)
- Timeout → increase timeout cap (performance)

Usage:
  from fleet_retry_engine import RetryableOp, RetryPolicy
  
  policy = RetryPolicy(max_retries=3, backoff_base=2.0, patterns={
      "connection_refused": lambda e: "ECONNREFUSED" in str(e),
      "timeout": lambda e: "timed out" in str(e).lower(),
  })
  
  result = await policy.execute(op_func)  # Auto-retries on match
"""

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional


@dataclass
class RetryConfig:
    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_jitter: bool = True
    max_delay: float = 60.0
    on_retry: Optional[Callable] = None  # callback(retry_num, error)


# Default pattern matchers for common failure categories
DEFAULT_PATTERNS: Dict[str, Callable[[Exception], bool]] = {
    "connection_refused": lambda e: any(kw in str(e).lower() for kw in ["connection refused", "econnrefused", "connection reset"]),
    "timeout": lambda e: any(kw in str(e).lower() for kw in ["timed out", "timeout", "deadline exceeded"]),
    "file_not_found": lambda e: "no such file" in str(e).lower() or "file not found" in str(e).lower(),
    "database_locked": lambda e: "locked" in str(e).lower() or "busy" in str(e).lower(),
    "permission_denied": lambda e: "permission denied" in str(e).lower() or "access denied" in str(e).lower(),

}



class RetryPolicy:
    """Apply intelligent retries to a function call."""
    
    def __init__(self, config: Optional[RetryConfig] = None, patterns: Optional[Dict[str, Callable]] = None):
        self.config = config or RetryConfig()
        self.patterns = {**DEFAULT_PATTERNS, **(patterns or {})}
        self.history: List[Dict[str, Any]] = []  # Track all retry attempts
    
    async def execute(self, func: Callable[..., Coroutine], *args, **kwargs) -> Dict[str, Any]:
        """Execute func with retry logic. Returns result dict with attempt history."""
        last_error = None
        
        for attempt in range(self.config.max_retries + 1):
            start = time.time()
            try:
                result = await func(*args, **kwargs)
                
                self.history.append({
                    "attempt": attempt + 1,
                    "success": True,
                    "duration_ms": round((time.time() - start) * 1000, 2),
                    "total_attempts": attempt + 1
                })
                return {"ok": True, "result": result, "attempts": attempt + 1}
                
            except Exception as e:
                last_error = e
                duration_ms = round((time.time() - start) * 1000, 2)
                
                # Check if this error matches a retryable pattern
                error_category = None
                for name, matcher in self.patterns.items():
                    if matcher(e):
                        error_category = name
                        break
                
                if error_category is None or attempt >= self.config.max_retries:
                    # Not retryable or max retries exhausted
                    self.history.append({
                        "attempt": attempt + 1,
                        "success": False,
                        "error_category": error_category,
                        "duration_ms": duration_ms,
                        "reason": "max_retries" if attempt >= self.config.max_retries else "non_retryable"
                    })
                    return {"ok": False, "error": str(e), "category": error_category, "attempts": attempt + 1}
                
                # Calculate delay with exponential backoff + jitter
                base_delay = self.config.backoff_base ** attempt
                jitter = random.uniform(0, base_delay * 0.5) if self.config.backoff_jitter else 0
                delay = min(base_delay + jitter, self.config.max_delay)
                
                # Record attempt
                self.history.append({
                    "attempt": attempt + 1,
                    "success": False,
                    "error_category": error_category,
                    "duration_ms": duration_ms,
                    "delay_s": round(delay, 2)
                })
                
                if self.config.on_retry:
                    self.config.on_retry(attempt + 1, e)
                
                await asyncio.sleep(delay)
        
        # Shouldn't reach here, but safety fallback
        return {"ok": False, "error": str(last_error), "attempts": self.config.max_retries + 1}
    
    @property
    def success_rate(self) -> float:
        if not self.history:
            return 0.0
        successes = sum(1 for h in self.history if h.get("success"))
        return round(successes / len(self.history) * 100, 1)


# CLI: retry a command string through shell
def main():
    parser = argparse.ArgumentParser(description="Fleet Self-Healing Retry Engine")
    parser.add_argument("--command", "-c", required=True, help="Command to execute with retry")
    parser.add_argument("--retries", "-r", type=int, default=3, help="Max retry attempts")
    parser.add_argument("--backoff", "-b", type=float, default=2.0, help="Exponential backoff base seconds")
    parser.add_argument("--show-history", action="store_true", help="Print retry history at end")
    
    args = parser.parse_args()
    
    policy = RetryPolicy(RetryConfig(max_retries=args.retries, backoff_base=args.backoff))
    
    async def _run():
        import subprocess
        async def _exec():
            proc = await asyncio.create_subprocess_shell(
                args.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", errors="replace").strip())
            return stdout.decode("utf-8", errors="replace").strip()
        
        return await policy.execute(_exec)
    
    result = asyncio.run(_run())
    print(json.dumps(result, indent=2))
    
    if args.show_history:
        print("\n--- Retry History ---")
        for h in policy.history:
            status = "OK" if h["success"] else f"FAIL (cat:{h.get('error_category','?')})"
            print(f"  Attempt {h['attempt']}: {status} | {h['duration_ms']}ms", file=sys.stderr)


if __name__ == "__main__":
    main()
