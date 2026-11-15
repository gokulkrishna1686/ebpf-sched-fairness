# Other supporting materials

`trace_validation/` holds the hand-checkable validation of the eBPF collector from the golden run:

- `validation_hand.md`: 20 individual waits of one thread, each with the wait computed from the raw `trace-cmd` events, the wait the eBPF collector recorded, their difference and the trace line numbers, followed by the raw trace lines for the first five, so the calculation can be followed by hand.
- `validation_waits.csv`: all 706 waits found in the independent `trace-cmd` recording, each matched to the eBPF measurement, with the difference in microseconds.

Result: 706 of 706 waits matched; median difference 0.12 µs, p99 2.1 µs, maximum 18.2 µs. They are extracted from `code/results/raw/validation_20261106-174432.tar.zst`, which also contains the full trace (`trace.dat`, `trace.txt`). `make reproduce` repeats this validation.
