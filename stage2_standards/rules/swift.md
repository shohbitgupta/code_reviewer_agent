# Swift / iOS Standards

Rules specific to **Swift** (iOS/macOS) code in this project.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## Swift Rules

### SW001 — No Force Unwrap on Optionals
- **Severity**: HIGH
- **Language**: swift
- **Category**: error_handling
- The `!` force-unwrap operator crashes at runtime when the optional is `nil`. Use `guard let`, `if let`, `??`, or `map` instead.
- **Bad:**
```swift
let name = user!.name!
```
- **Good:**
```swift
guard let user = user, let name = user.name else { return }
```

### SW002 — Use guard for Early Exit
- **Severity**: MEDIUM
- **Language**: swift
- **Category**: style
- Use `guard let` / `guard` for precondition checks at the top of a function. This keeps the happy path un-indented and exits early on failure.
- **Bad:**
```swift
func process(data: Data?) {
    if let data = data {
        if data.count > 0 {
            // main logic deeply nested
        }
    }
}
```
- **Good:**
```swift
func process(data: Data?) {
    guard let data = data, !data.isEmpty else { return }
    // main logic at top level
}
```

### SW003 — Avoid Massive ViewControllers
- **Severity**: MEDIUM
- **Language**: swift
- **Category**: architecture
- ViewControllers exceeding 300 lines are a sign that business logic, data fetching, or view configuration has not been separated. Extract to ViewModels, coordinators, or services.
- **Bad:**
```swift
class ProfileViewController: UIViewController {
    // 500 lines: networking, caching, layout, analytics ...
}
```
- **Good:**
```swift
class ProfileViewController: UIViewController {
    var viewModel: ProfileViewModel! // all business logic here
}
```

### SW004 — Use [weak self] in Closures to Prevent Retain Cycles
- **Severity**: HIGH
- **Language**: swift
- **Category**: error_handling
- Closures that capture `self` strongly create retain cycles when stored as properties, leading to memory leaks. Use `[weak self]` and guard against nil.
- **Bad:**
```swift
networkService.fetch { response in
    self.updateUI(with: response) // strong capture — retain cycle
}
```
- **Good:**
```swift
networkService.fetch { [weak self] response in
    guard let self = self else { return }
    self.updateUI(with: response)
}
```

### SW005 — No try! — Always Handle Thrown Errors
- **Severity**: HIGH
- **Language**: swift
- **Category**: error_handling
- `try!` crashes at runtime if the call throws. Use `try` inside a `do/catch` block or propagate with `throws`.
- **Bad:**
```swift
let data = try! Data(contentsOf: url)
```
- **Good:**
```swift
do {
    let data = try Data(contentsOf: url)
    process(data)
} catch {
    logger.error("Failed to load data: \(error)")
}
```

### SW006 — Prefer Structs Over Classes for Value Semantics
- **Severity**: LOW
- **Language**: swift
- **Category**: style
- Swift structs have value semantics (copy-on-write), making them safer and faster for data models. Use classes only when reference identity or inheritance is required.
- **Bad:**
```swift
class Point { var x: Double; var y: Double }
```
- **Good:**
```swift
struct Point { let x: Double; let y: Double }
```

### SW007 — Mark Protocol Conformances in Extensions
- **Severity**: INFO
- **Language**: swift
- **Category**: style
- Conform to protocols in separate `extension` blocks rather than in the primary type declaration. This improves readability and makes conformances easy to locate.
- **Good:**
```swift
class UserCell: UITableViewCell { /* core class body */ }

extension UserCell: Configurable {
    func configure(with user: User) { ... }
}
```

### SW008 — No Direct Network Calls from ViewControllers (Clean Architecture)
- **Severity**: HIGH
- **Language**: swift
- **Category**: architecture
- ViewControllers belong to the UI layer. Calling `URLSession`, `URLRequest`, or any networking API directly inside a ViewController couples the UI to the network layer, violates Clean Architecture, and makes the screen impossible to unit-test without a live network. All network calls must go through a dedicated service or repository class.
- **Bad:**
```swift
class ContactsViewController: UIViewController {
    func loadData() {
        URLSession.shared.dataTask(with: url) { data, _, _ in
            // parsing + UI update mixed in the VC
        }.resume()
    }
}
```
- **Good:**
```swift
class ContactsViewController: UIViewController {
    var contactService: ContactServiceProtocol!

    func loadData() {
        contactService.fetchContacts { [weak self] result in
            self?.updateUI(with: result)
        }
    }
}
```
