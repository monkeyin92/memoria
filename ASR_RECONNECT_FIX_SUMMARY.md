# ASR Reconnect `_accepted_text` Memory Leak Fix

## Summary
Fixed a memory leak and potential incorrect prefix matching bug in `ASRStreamSupervisor.reconnect()` where `_accepted_text` dictionary was not cleared when reconnecting to a new stream epoch.

## Root Cause
In `services/agent/src/voice_core/asr_stream_supervisor.py`, the `reconnect()` method (lines ~525-546) cleared all state dictionaries except `_accepted_text`:

```python
def reconnect(self, *, stream_epoch: int) -> bool:
    # ... validation ...
    self._revisions.clear()
    self._final_intervals.clear()
    self._final_interval_order.clear()
    self._latest_task_by_segment.clear()
    self._revision_order.clear()
    self._segment_order.clear()
    # ❌ BUG: _accepted_text was NOT cleared
    self.timeline.start_stream_epoch(stream_epoch)
    return True
```

## Impact

### 1. Memory Leak
Text from previous stream epochs accumulated indefinitely in `_accepted_text`. Each reconnect added more entries without removing old ones, causing unbounded memory growth.

**Evidence from reproduction test:**
```
Stream 1: accepted 3 results, _accepted_text has 3 entries
Stream 2: accepted 3 results, _accepted_text has 6 entries  ← accumulated
Stream 3: accepted 3 results, _accepted_text has 9 entries  ← accumulated
```

### 2. Potential Incorrect Prefix Matching
The `_untimed_tail_after_committed_prefix()` method (line ~217) looks up prefixes using `(stream_epoch, task_epoch, capture_start_sample)` as keys. If old stream epochs' text remained and `capture_start_sample` values collided across different streams, the method could match against stale text from a previous stream.

## Fix
Added `self._accepted_text.clear()` to the `reconnect()` method:

```python
def reconnect(self, *, stream_epoch: int) -> bool:
    # ... validation ...
    self._revisions.clear()
    self._final_intervals.clear()
    self._final_interval_order.clear()
    self._latest_task_by_segment.clear()
    self._revision_order.clear()
    self._segment_order.clear()
    self._accepted_text.clear()  # ✅ FIX: Clear accepted text on reconnect
    self.timeline.start_stream_epoch(stream_epoch)
    return True
```

**Location:** `services/agent/src/voice_core/asr_stream_supervisor.py`, line 546 (added one line)

## Verification

### Reproduction Test Results (Before Fix)
```
✗ BUG CONFIRMED: _accepted_text still has 1 entries after reconnect!
  Old stream_epoch=1 data persists in _accepted_text

✗ BUG: Text accumulated across 3 streams!
  Expected: ≤3 entries (current stream only)
  Actual: 9 entries (accumulated)
```

### After Fix
```
✓ BUG FIXED: _accepted_text was properly cleared on reconnect

✓ No accumulation: only current stream entries remain
```

### Regression Test
Created `services/agent/tests/unit/test_asr_reconnect_accepted_text_clear.py` with 3 test cases:
1. `test_accepted_text_cleared_on_reconnect` - Verifies the dict is empty after reconnect
2. `test_no_cross_stream_text_accumulation` - Tests multiple reconnects don't accumulate
3. `test_reconnect_clears_all_state` - Ensures consistent clearing of all state

All tests pass: `3 passed in 0.04s`

### Existing Tests
Ran existing ASR-related tests to ensure no regressions:
- `test_funasr_protocol.py`: 13 passed
- `test_asr_failover.py`: 38 passed
- `test_speech_timeline.py`: 8 passed

**Total: 46 existing tests pass ✓**

## Data Structure Reference

`_accepted_text` is defined at line 112-115:
```python
_accepted_text: dict[tuple[int, int, int], tuple[int, str]] = field(
    default_factory=dict,
    init=False,
)
# Key: (stream_epoch, task_epoch, capture_start_sample)
# Value: (capture_end_sample, text)
```

It is populated in `accept_result()` at lines 485-489 and used in `_untimed_tail_after_committed_prefix()` at line 221.

## Related Code Paths

### Population
`accept_result()` → line 485-489:
```python
text_key = (result.stream_epoch, result.task_epoch, result.capture_start_sample)
self._accepted_text.pop(text_key, None)
self._accepted_text[text_key] = (result.capture_end_sample, result.text)
```

### Consumption
`_untimed_tail_after_committed_prefix()` → line 221-223:
```python
prefix = self._accepted_text.get(
    (result.stream_epoch, result.task_epoch, result.capture_start_sample)
)
```

## Crisis Priority Preservation
✅ No crisis routing logic was modified
✅ No text trimming or character limitations were altered
✅ Fix is minimal and surgical - single line addition
✅ All crisis-related behaviors remain unchanged

## Files Changed

1. **Source Code (1 line added)**
   - `services/agent/src/voice_core/asr_stream_supervisor.py`
     - Line 546: Added `self._accepted_text.clear()`

2. **Tests (new file)**
   - `services/agent/tests/unit/test_asr_reconnect_accepted_text_clear.py`
     - 5168 bytes, 3 regression tests

3. **Documentation (this file)**
   - `ASR_RECONNECT_FIX_SUMMARY.md`

## Remaining Risks
✅ **None identified** - The fix is complete and minimal:
- Clears state consistently with all other reconnect cleanup
- Tested with dedicated regression tests
- All existing tests continue to pass
- No behavior changes for normal operation
- Memory leak eliminated
- Stale prefix matching prevented

## Cleanup
Temporary reproduction script can be removed:
- `test_asr_leak_repro.py` (root directory) - used for initial bug confirmation
- `reproduce_asr_leak.py` (root directory) - unused scaffold file

The permanent regression test is in the proper test directory.
