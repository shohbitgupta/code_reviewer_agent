# Python Standards

Rules specific to **Python** code in this project.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## Python Rules

### PY001 — Type Hints on Public Functions
- **Severity**: LOW
- **Language**: python
- **Category**: style
- All public functions and methods (those not prefixed with `_`) must have type annotations on every parameter and on the return type.
- **Bad:**
```python
def fetch_user(user_id):
    ...
```
- **Good:**
```python
def fetch_user(user_id: int) -> Optional[User]:
    ...
```

### PY002 — No Bare except
- **Severity**: HIGH
- **Language**: python
- **Category**: error_handling
- Never use a bare `except:` clause. It silently swallows `SystemExit`, `KeyboardInterrupt`, and every other exception, making debugging nearly impossible.
- **Bad:**
```python
try:
    connect()
except:
    pass
```
- **Good:**
```python
try:
    connect()
except ConnectionError as e:
    logger.warning("Connection failed: %s", e)
```

### PY003 — No Mutable Default Arguments
- **Severity**: HIGH
- **Language**: python
- **Category**: style
- Never use a mutable object (list, dict, set) as a default parameter value. The same object is shared across all calls, causing subtle state-leakage bugs.
- **Bad:**
```python
def append_item(item, container=[]):
    container.append(item)
    return container
```
- **Good:**
```python
def append_item(item, container=None):
    if container is None:
        container = []
    container.append(item)
    return container
```

### PY004 — No Wildcard Imports
- **Severity**: MEDIUM
- **Language**: python
- **Category**: style
- `from module import *` pollutes the namespace, makes it impossible to determine the origin of names, and breaks static analysis.
- **Bad:**
```python
from os.path import *
from models import *
```
- **Good:**
```python
from os.path import join, exists
from models import User, Order
```

### PY005 — Use f-strings for String Interpolation
- **Severity**: INFO
- **Language**: python
- **Category**: style
- Prefer f-strings over `%` formatting or `.format()`. f-strings are faster, more readable, and catch name errors at parse time.
- **Bad:**
```python
msg = "Hello, %s! You have %d messages." % (name, count)
msg = "Hello, {}! You have {} messages.".format(name, count)
```
- **Good:**
```python
msg = f"Hello, {name}! You have {count} messages."
```

### PY006 — No print() in Production Code
- **Severity**: MEDIUM
- **Language**: python
- **Category**: style
- Use the `logging` module instead of `print()`. `print()` cannot be toggled by log level, has no timestamps, and is not captured by log aggregators.
- **Bad:**
```python
print(f"Processing order {order_id}")
```
- **Good:**
```python
logger.info("Processing order %s", order_id)
```

### PY007 — Dataclasses Over Plain Dicts for Structured Data
- **Severity**: LOW
- **Language**: python
- **Category**: style
- Use `@dataclass` or named tuples instead of bare dicts when a structure has a fixed schema. This enables type checking and IDE autocomplete.
- **Bad:**
```python
user = {"id": 1, "name": "Alice", "email": "alice@example.com"}
```
- **Good:**
```python
@dataclass
class User:
    id: int
    name: str
    email: str
```
