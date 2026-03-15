# Rust Standards

Rules specific to **Rust** code in this project.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## Rust Rules

### RS001 — No unwrap()/expect() Outside Tests
- **Severity**: HIGH
- **Language**: rust
- **Category**: error_handling
- `unwrap()` and `expect()` panic at runtime on `None` or `Err`. Outside of tests and prototypes, propagate errors with `?` or handle them explicitly.
- **Bad:**
```rust
let config = read_config().unwrap();
```
- **Good:**
```rust
let config = read_config()?;
```

### RS002 — Use Result<T, E> for Fallible Operations
- **Severity**: HIGH
- **Language**: rust
- **Category**: error_handling
- Functions that can fail must return `Result<T, E>`, not panic. Use `thiserror` or `anyhow` to define and wrap error types.
- **Bad:**
```rust
fn parse_port(s: &str) -> u16 {
    s.parse().expect("invalid port")
}
```
- **Good:**
```rust
fn parse_port(s: &str) -> Result<u16, ParseIntError> {
    s.parse()
}
```

### RS003 — Unsafe Blocks Must Have a Safety Comment
- **Severity**: CRITICAL
- **Language**: rust
- **Category**: security
- Every `unsafe` block must be preceded by a `// SAFETY:` comment that explains why the invariants required by the unsafe code are upheld.
- **Bad:**
```rust
unsafe {
    *ptr = value;
}
```
- **Good:**
```rust
// SAFETY: `ptr` is guaranteed non-null and exclusively owned here because
// it was obtained from `Box::into_raw` above and has not been aliased.
unsafe {
    *ptr = value;
}
```

### RS004 — Avoid Unnecessary Cloning
- **Severity**: MEDIUM
- **Language**: rust
- **Category**: complexity
- Cloning data structures to work around borrow-checker issues often signals a design problem. Prefer references, `Rc`/`Arc`, or restructuring ownership.
- **Bad:**
```rust
let name = user.name.clone();
process(name);
process(user.name.clone()); // cloned again
```
- **Good:**
```rust
process(&user.name);
```

### RS005 — Derive Common Traits for Data Types
- **Severity**: LOW
- **Language**: rust
- **Category**: style
- Plain data structs should derive `Debug`, `Clone`, `PartialEq` (and `Eq` / `Hash` when appropriate). This is zero-cost and enables testing and debugging.
- **Bad:**
```rust
struct Config {
    host: String,
    port: u16,
}
```
- **Good:**
```rust
#[derive(Debug, Clone, PartialEq)]
struct Config {
    host: String,
    port: u16,
}
```

### RS006 — No Panics in Library Code
- **Severity**: HIGH
- **Language**: rust
- **Category**: error_handling
- Library crates must never call `panic!`, `todo!`, `unimplemented!`, or `unreachable!` in paths reachable from the public API. Let callers decide how to handle errors.
- **Bad:**
```rust
pub fn divide(a: i64, b: i64) -> i64 {
    if b == 0 { panic!("division by zero"); }
    a / b
}
```
- **Good:**
```rust
pub fn divide(a: i64, b: i64) -> Result<i64, DivisionError> {
    if b == 0 { return Err(DivisionError::DivideByZero); }
    Ok(a / b)
}
```

### RS007 — Prefer Iterators Over Manual Index Loops
- **Severity**: INFO
- **Language**: rust
- **Category**: style
- Rust iterators are zero-cost, expressive, and avoid bounds-check bugs. Prefer `.iter()`, `.map()`, `.filter()` over index-based `for i in 0..len` loops.
- **Bad:**
```rust
for i in 0..items.len() {
    println!("{}", items[i]);
}
```
- **Good:**
```rust
for item in &items {
    println!("{item}");
}
```
