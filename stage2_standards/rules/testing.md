# Testing & Coverage Standards

Rules that apply to **all languages** in this project.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## Testing Rules

### TEST001 — Public Business-Logic Function Without Adjacent Test Coverage
- **Severity**: MEDIUM
- **Language**: all
- **Category**: testing
- A public function that implements real business logic (branching, calculations, state transitions — not a trivial getter/passthrough) and has no evident companion test anywhere in the codebase is a coverage gap. Flag it as worth adding a test for, rather than assuming absence of evidence means a test exists elsewhere.
- **Bad:**
```python
def apply_discount(order, coupon):
    if coupon.expired:
        raise ValueError("expired coupon")
    return order.total * (1 - coupon.percent_off)
# no test_apply_discount anywhere in the reviewed context
```
- **Good:**
```python
def apply_discount(order, coupon):
    if coupon.expired:
        raise ValueError("expired coupon")
    return order.total * (1 - coupon.percent_off)

def test_apply_discount_rejects_expired_coupon():
    ...
```

### TEST002 — Test Asserts Only "No Exception" Instead of Real Behavior
- **Severity**: MEDIUM
- **Language**: all
- **Category**: testing
- A test that only calls a function and asserts it didn't raise (or asserts a trivially-true condition) doesn't verify the function actually did the right thing. Assert on the specific return value, state change, or side effect the function is supposed to produce.
- **Bad:**
```python
def test_apply_discount():
    apply_discount(order, coupon)  # no assertion on the result at all
```
- **Good:**
```python
def test_apply_discount():
    result = apply_discount(order, coupon)
    assert result == pytest.approx(order.total * 0.9)
```

### TEST003 — Missing Boundary-Condition Coverage
- **Severity**: LOW
- **Language**: all
- **Category**: testing
- Code with an explicit boundary (empty input, zero, negative, max-length, off-by-one-prone loop bound) whose adjacent tests only cover the "happy path" middle case is missing the coverage that would actually catch a regression at that boundary.
- **Bad:**
```python
def test_apply_discount():
    apply_discount(make_order(total=100), make_coupon(percent_off=0.1))
    # no test for total=0, percent_off=0, or percent_off=1.0
```
- **Good:**
```python
@pytest.mark.parametrize("total,percent_off", [(100, 0.1), (0, 0.1), (100, 0.0), (100, 1.0)])
def test_apply_discount_boundaries(total, percent_off):
    ...
```

### TEST004 — Over-Mocked Test That Doesn't Exercise Real Logic
- **Severity**: LOW
- **Language**: all
- **Category**: testing
- A test that mocks the function or module under test itself (rather than only its external dependencies — network, DB, filesystem) passes regardless of whether the real logic is correct, since it's asserting against the mock's canned behavior, not the actual implementation.
- **Bad:**
```python
def test_apply_discount(mocker):
    mocker.patch("module.apply_discount", return_value=90)
    assert module.apply_discount(order, coupon) == 90  # tests the mock, not the code
```
- **Good:**
```python
def test_apply_discount(mocker):
    mocker.patch("module.payment_gateway.charge")  # mock only the external dependency
    result = module.apply_discount(order, coupon)
    assert result == pytest.approx(order.total * 0.9)
```
