# Performance Standards

Rules that apply to **all languages** in this project.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## Performance Rules

### PERF001 — No Repeated Remote Calls Inside a Loop (N+1)
- **Severity**: HIGH
- **Language**: all
- **Category**: performance
- Making a network, database, or other remote call once per loop iteration (an "N+1" pattern) turns a single round trip into N. Batch the calls, fetch the full collection up front, or restructure to a single query keyed on the loop's identifiers.
- **Bad:**
```python
for order_id in order_ids:
    order = db.query(f"SELECT * FROM orders WHERE id = {order_id}")
    process(order)
```
- **Good:**
```python
orders = db.query("SELECT * FROM orders WHERE id IN (?)", order_ids)
for order in orders:
    process(order)
```

### PERF002 — No Blocking I/O on the Main/UI Thread
- **Severity**: HIGH
- **Language**: all
- **Category**: performance
- File, network, or database calls made synchronously on the main/UI thread freeze the interface (or, on a server, block the event loop) for every other request until the call returns. Move blocking work to a background thread, isolate, or async task.
- **Bad:**
```dart
void onPressed() {
  final data = File(path).readAsStringSync(); // blocks the UI thread
  setState(() => text = data);
}
```
- **Good:**
```dart
Future<void> onPressed() async {
  final data = await File(path).readAsString();
  setState(() => text = data);
}
```

### PERF003 — No Unbounded Allocation in Hot Loops or Recursion
- **Severity**: MEDIUM
- **Language**: all
- **Category**: performance
- Allocating a new collection, object, or string on every iteration of a hot loop (or every call of a frequently-invoked recursive function) creates avoidable GC/allocator pressure. Hoist the allocation out of the loop, reuse a buffer, or restructure to avoid per-iteration allocation.
- **Bad:**
```python
def total(items):
    result = 0
    for item in items:
        temp = [x * 2 for x in item.values]  # new list every iteration, discarded immediately
        result += sum(temp)
    return result
```
- **Good:**
```python
def total(items):
    return sum(x * 2 for item in items for x in item.values)
```

### PERF004 — Memoize Expensive Pure Computations Instead of Recomputing
- **Severity**: MEDIUM
- **Language**: all
- **Category**: performance
- A pure, expensive computation (parsing, complex aggregation, a costly pure function) called repeatedly with the same inputs should cache its result rather than recomputing every call. Applies especially inside a UI `build()`/render method or a request handler invoked per-item in a loop.
- **Bad:**
```python
def render(self):
    stats = compute_expensive_stats(self.dataset)  # recomputed on every render
    return format(stats)
```
- **Good:**
```python
def render(self):
    if self._stats_cache is None:
        self._stats_cache = compute_expensive_stats(self.dataset)
    return format(self._stats_cache)
```

### PERF005 — Avoid O(n²) Collection Operations Where O(n) Is Available
- **Severity**: MEDIUM
- **Language**: all
- **Category**: performance
- Searching a list inside a loop (`in`, `.find`, nested iteration) turns a linear scan into a quadratic one. Use a set/dict/hash-map lookup built once outside the loop when membership or key lookup is needed repeatedly.
- **Bad:**
```python
def annotate(items, allowed_ids):
    return [i for i in items if i.id in allowed_ids]  # allowed_ids is a list: O(n*m)
```
- **Good:**
```python
def annotate(items, allowed_ids):
    allowed = set(allowed_ids)
    return [i for i in items if i.id in allowed]
```
