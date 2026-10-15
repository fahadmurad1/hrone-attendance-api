# DECISIONS.md

1. **Indexes.**
We created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` to enforce identity uniqueness and race-safe operations. We created compound indexes on `attendance_logs.(date DESC, emp_code ASC)` for list sorting/filtering without in-memory sort, `attendance_logs.(emp_code ASC, date DESC)` for fast punch-outs, and `employees.(department ASC, emp_code ASC)`. We considered and rejected an index on `(emp_code, punch_in)` because `(emp_code, date)` already serves employee queries without redundant write overhead.

2. **Punch-in race.**
When two identical punch-in requests arrive concurrently, both compute the attendance date and attempt insertion. MongoDB's compound unique index `(emp_code, date)` allows exactly one to succeed; the other raises a `DuplicateKeyError`, which our exception handler catches and returns as HTTP 409 Conflict.

3. **Ties.**
We use MongoDB's `$rank` window function (`$setWindowFields`) for standard competition ranking (1, 2, 2, 4). The `limit` applies after ranking (`rank <= limit`). Tied employees share the rank and are both returned, ordered secondary by `emp_code` ASC.

4. **Headcount.**
Department summary starts from `employees` filtered by `joined_on <= month_end`, grouping by department for baseline headcount (`$sum: 1`). Attendance logs are joined via `$lookup`, so employees with zero logs are fully preserved in headcount.

5. **One thing you would change for 100x data.**
At 100x scale (~10M records), we would maintain pre-aggregated daily/monthly rollups via asynchronous Change Streams rather than running ad-hoc cross-collection aggregations over raw punch records.

