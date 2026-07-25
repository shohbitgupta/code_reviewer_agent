# General & Security Standards

Rules that apply to **all languages** in this project.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## General Rules

### GEN001 — Function Length
- **Severity**: MEDIUM
- **Language**: all
- **Category**: complexity
- Functions and methods must not exceed 50 lines of executable code (blank lines and comment-only lines excluded). Long functions are a strong signal that the function is doing too many things.
- **Bad:**
```python
def process_order(order):
    # 80 lines of mixed validation, DB writes, email sending, logging ...
```
- **Good:**
```python
def process_order(order):
    _validate_order(order)
    _persist_order(order)
    _notify_customer(order)
```

### GEN002 — No Magic Numbers
- **Severity**: LOW
- **Language**: all
- **Category**: style
- Numeric literals (other than 0, 1, -1) must not appear inline. Extract to named constants or configuration values.
- **Bad:**
```kotlin
if (retryCount > 3) { ... }
```
- **Good:**
```kotlin
const val MAX_RETRY_COUNT = 3
if (retryCount > MAX_RETRY_COUNT) { ... }
```

### GEN003 — No TODO/FIXME in Committed Code
- **Severity**: LOW
- **Language**: all
- **Category**: style
- TODO and FIXME comments must not appear in committed production code. File a tracking ticket instead and reference the ticket ID in a comment if needed.
- **Bad:**
```python
# TODO: handle edge case here
```
- **Good:**
```python
# PROJ-421: edge case handled in next sprint
```

### GEN004 — Cyclomatic Complexity ≤ 10
- **Severity**: HIGH
- **Language**: all
- **Category**: complexity
- No function should contain more than 10 independent paths (if/elif/else/for/while/try/case branches each add 1). High complexity makes testing and reasoning about code extremely difficult.
- **Bad:**
```python
def classify(x):
    if x > 100:
        if x > 200:
            if x % 2 == 0: ...
            else: ...
        elif x > 150: ...
    elif x > 50: ...
    # ... 8 more branches
```
- **Good:** Extract branches into helper functions with clear names.

### GEN005 — Maximum Nesting Depth ≤ 4
- **Severity**: MEDIUM
- **Language**: all
- **Category**: complexity
- Code must not be nested more than 4 levels deep (function body is level 1). Excessive nesting hides control flow and makes code hard to test.
- **Bad:**
```kotlin
fun process() {
    if (a) {
        for (b in list) {
            if (c) {
                while (d) {
                    if (e) { /* level 5 */ }
                }
            }
        }
    }
}
```
- **Good:** Extract inner logic into named functions.

---

## Security Rules

### SEC001 — No Hardcoded Secrets
- **Severity**: CRITICAL
- **Language**: all
- **Category**: security
- API keys, passwords, tokens, private keys, and connection strings must never appear in source code. Use environment variables or a secrets manager.
- **Bad:**
```python
API_KEY = "sk-abc123XYZ"
DB_PASSWORD = "hunter2"
```
- **Good:**
```python
API_KEY = os.environ["API_KEY"]
```

### SEC002 — No Raw SQL String Concatenation
- **Severity**: CRITICAL
- **Language**: all
- **Category**: security
- SQL queries must never be built by concatenating or formatting user-controlled strings. Use parameterised queries or an ORM.
- **Bad:**
```python
cursor.execute("SELECT * FROM users WHERE id = " + user_id)
```
- **Good:**
```python
cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
```

### SEC003 — No Logging of Sensitive Data
- **Severity**: HIGH
- **Language**: all
- **Category**: security
- Passwords, tokens, PII (names, emails, phone numbers), and financial data must never be written to logs.
- **Bad:**
```python
logger.info("User login: %s / %s", username, password)
```
- **Good:**
```python
logger.info("User login attempt for user_id=%s", user_id)
```

### SEC004 — Validate All External Input
- **Severity**: HIGH
- **Language**: all
- **Category**: security
- Data arriving from HTTP requests, files, environment variables, or inter-process communication must be validated and sanitised before use.
- **Bad:**
```python
filename = request.args.get("file")
open(filename)  # path traversal risk
```
- **Good:**
```python
filename = secure_filename(request.args.get("file", ""))
if not filename:
    abort(400)
```
