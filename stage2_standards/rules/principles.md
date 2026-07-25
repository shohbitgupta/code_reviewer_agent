# Common Principles: SOLID, Clean Architecture & Design Patterns

Universal rules that apply to **all languages** in this project.
These principles transcend language syntax — they govern how responsibilities are divided,
how dependencies flow between layers, and which structural patterns lead to maintainable code.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## SOLID Principles

### CP001 — Single Responsibility Principle (SRP)
- **Severity**: MEDIUM
- **Language**: all
- **Category**: architecture
- Every class, module, or function should have exactly one reason to change. A class that fetches data, transforms it, sends emails, and updates the UI is doing four jobs — any change to any concern requires touching (and retesting) the same class.
- **Bad:**
```python
class UserManager:
    def fetch_from_db(self): ...
    def send_welcome_email(self): ...
    def export_to_csv(self): ...
    def render_profile_page(self): ...
```
- **Good:**
```python
class UserRepository:
    def fetch(self, user_id: int) -> User: ...

class UserNotifier:
    def send_welcome_email(self, user: User): ...
```

### CP002 — Open/Closed Principle (OCP)
- **Severity**: MEDIUM
- **Language**: all
- **Category**: architecture
- Classes and functions should be open for extension but closed for modification. Adding new behaviour should not require editing existing, tested code. Growing if/switch chains keyed on type strings is the canonical violation — use polymorphism, strategy, or the decorator pattern instead.
- **Bad:**
```python
def calculate_discount(order, customer_type: str) -> float:
    if customer_type == "vip":
        return order.total * 0.20
    elif customer_type == "regular":
        return order.total * 0.05
    # Adding "premium" means editing this function and its tests
```
- **Good:**
```python
class DiscountPolicy(ABC):
    @abstractmethod
    def apply(self, order: Order) -> float: ...

class VipDiscount(DiscountPolicy):
    def apply(self, order: Order) -> float: return order.total * 0.20

class RegularDiscount(DiscountPolicy):
    def apply(self, order: Order) -> float: return order.total * 0.05
```

### CP003 — Liskov Substitution Principle (LSP)
- **Severity**: HIGH
- **Language**: all
- **Category**: architecture
- Any subtype must be substitutable for its base type without breaking the program. Overriding a method to throw an exception, return a meaningless stub, or tighten preconditions violates LSP and signals the inheritance hierarchy is wrong. Prefer composition over inheritance when substitutability cannot be guaranteed.
- **Bad:**
```kotlin
open class Bird { open fun fly() { /* flap */ } }
class Penguin : Bird() {
    override fun fly() { throw UnsupportedOperationException("penguins can't fly") }
}
```
- **Good:**
```kotlin
interface Flyable { fun fly() }
class Sparrow : Flyable { override fun fly() { /* flap */ } }
class Penguin   // does not implement Flyable — the hierarchy now tells the truth
```

### CP004 — Interface Segregation Principle (ISP)
- **Severity**: MEDIUM
- **Language**: all
- **Category**: architecture
- Clients must not be forced to depend on methods they do not use. A fat interface or protocol with many unrelated methods forces every implementor to provide stubs for irrelevant operations. Break large interfaces into focused, role-specific ones.
- **Bad:**
```swift
protocol UserService {
    func fetchUser(id: String) -> User
    func saveUser(_ user: User)
    func deleteUser(id: String)
    func sendPasswordResetEmail(to email: String)
    func generateInvoice(for user: User) -> Invoice
    func exportToCsv() -> Data
}
```
- **Good:**
```swift
protocol UserRepository { func fetch(id: String) -> User; func save(_ u: User); func delete(id: String) }
protocol UserNotifier   { func sendPasswordReset(to email: String) }
protocol InvoiceService { func generate(for user: User) -> Invoice }
```

### CP005 — Dependency Inversion Principle (DIP)
- **Severity**: HIGH
- **Language**: all
- **Category**: architecture
- High-level modules must not depend on low-level modules. Both should depend on abstractions (interfaces/protocols/abstract classes). Instantiating a concrete class directly inside a high-level module hard-codes the dependency and makes unit testing impossible without a real database, network, or third-party SDK.
- **Bad:**
```kotlin
class OrderProcessor {
    private val db     = MySQLOrderRepository()   // concrete — impossible to swap or mock
    private val mailer = SendGridMailer()
}
```
- **Good:**
```kotlin
class OrderProcessor(
    private val db:     OrderRepository,          // depends on abstraction
    private val mailer: MailerService,            // injected — mockable in tests
)
```

---

## Clean Architecture

### CP006 — Enforce Layer Boundaries
- **Severity**: HIGH
- **Language**: all
- **Category**: architecture
- Dependencies must flow strictly inward: Presentation → Application → Domain → Infrastructure (never the reverse). A ViewController making a direct network call, a domain entity importing a UI type, or a use case querying the database directly are all layer-boundary violations. Each violation makes the violated layer impossible to test in isolation.
- **Bad:**
```swift
class ContactsViewController: UIViewController {
    func loadContacts() {
        URLSession.shared.dataTask(with: url) { ... }.resume() // UI calling Infrastructure directly
    }
}
```
- **Good:**
```swift
class ContactsViewController: UIViewController {
    var contactService: ContactServiceProtocol!   // injected Application-layer abstraction
    func loadContacts() { contactService.fetch { [weak self] result in self?.update(result) } }
}
```

### CP007 — Repository Pattern: Persistence Logic Belongs in Repositories
- **Severity**: MEDIUM
- **Language**: all
- **Category**: architecture
- SQL queries, ORM calls, file I/O, cache reads, and remote API calls must live exclusively in repository or data-source classes. Business logic classes (services, use cases, ViewModels) must receive domain objects from repositories — not raw database rows or HTTP responses.
- **Bad:**
```kotlin
class OrderService(val db: Database) {
    fun getOrder(id: String): Order {
        return db.rawQuery("SELECT * FROM orders WHERE id = ?", id)  // persistence in a service
    }
}
```
- **Good:**
```kotlin
class OrderService(val repo: OrderRepository) {
    fun getOrder(id: String): Order = repo.findById(id)  // service talks to abstraction
}
```

### CP008 — Domain Entities Must Be Framework-Free
- **Severity**: MEDIUM
- **Language**: all
- **Category**: architecture
- Core domain objects (entities, value objects, aggregates) must not import or reference any UI framework, networking library, persistence library, or third-party SDK. They represent pure business concepts and must be independently testable and portable.
- **Bad:**
```swift
import UIKit
struct Contact {       // domain entity coupled to UIKit
    var name: String
    var avatar: UIImageView   // framework type in domain model
}
```
- **Good:**
```swift
struct Contact {       // pure domain entity — no framework imports
    let id: UUID
    let name: String
    let avatarURL: URL?
}
```

### CP009 — Use Cases Must Not Contain Presentation Logic
- **Severity**: HIGH
- **Language**: all
- **Category**: architecture
- A use case (interactor) orchestrates business rules and returns domain data. It must not format strings for display, assign colours, navigate between screens, or call any layout API. Presentation decisions belong exclusively in the presentation layer.
- **Bad:**
```kotlin
class FetchContactsUseCase {
    fun execute(): String {
        val contacts = repo.getAll()
        return "<b>${contacts.size} contacts found</b>"  // HTML formatting in business logic
    }
}
```
- **Good:**
```kotlin
class FetchContactsUseCase {
    fun execute(): List<Contact> = repo.getAll()   // returns domain objects, not formatted strings
}
```

### CP010 — Avoid Circular Dependencies Between Modules
- **Severity**: HIGH
- **Language**: all
- **Category**: architecture
- Module A must not import Module B if Module B already (directly or transitively) imports Module A. Circular dependencies prevent independent compilation, testing, and deployment of modules. Break them by extracting the shared concept to a third module or by introducing an interface that one side depends on.
- **Bad:**
```
OrderService   imports PaymentService
PaymentService imports OrderService   ← circular — neither can be compiled alone
```
- **Good:**
```
PaymentPort    (interface in Domain layer)
OrderService   imports PaymentPort
PaymentService implements PaymentPort   ← no cycle
```

---

## Design Pattern Anti-patterns

### CP011 — Avoid Singleton Overuse
- **Severity**: MEDIUM
- **Language**: all
- **Category**: architecture
- Singletons are global mutable state. Every caller implicitly depends on the same instance, making unit testing, concurrency reasoning, and lifecycle management difficult. Reserve singletons for truly global, stateless resources. For services, repositories, and configuration: use dependency injection instead.
- **Bad:**
```swift
class Database {
    static let shared = Database()   // global mutable state accessible everywhere
    private init() {}
}
func processOrder() { Database.shared.save(order) }   // hidden dependency
```
- **Good:**
```swift
class OrderProcessor {
    init(db: DatabaseProtocol) { self.db = db }   // dependency is explicit and injectable
}
```

### CP012 — Avoid God Class
- **Severity**: HIGH
- **Language**: all
- **Category**: architecture
- A class with more than 10 public methods or 300+ lines that touches many unrelated concerns is a God Class. It accumulates responsibilities over time, becomes the hardest class to change, and makes targeted testing impossible. Extract cohesive groups of methods into separate, focused classes.
- **Bad:**
```kotlin
class AppManager {
    fun loginUser() { ... }
    fun fetchOrders() { ... }
    fun sendPushNotification() { ... }
    fun cacheImage() { ... }
    fun exportReport() { ... }
    // 15 more unrelated methods
}
```
- **Good:** `UserSession`, `OrderRepository`, `PushService`, `ImageCache`, `ReportExporter` — each focused, each independently testable.

### CP013 — Avoid Long Parameter Lists (> 5 Parameters)
- **Severity**: MEDIUM
- **Language**: all
- **Category**: complexity
- Functions or methods with more than 5 parameters are hard to call correctly, hard to test, and signal that the function is doing too much. Group related parameters into a value object (struct / data class / record) or split the function by responsibility.
- **Bad:**
```python
def create_user(name, email, age, role, department, country, locale, timezone):
    ...
```
- **Good:**
```python
@dataclass
class UserProfile:
    name: str; email: str; age: int; role: str
    department: str; country: str; locale: str; timezone: str

def create_user(profile: UserProfile): ...
```

### CP014 — Avoid Primitive Obsession
- **Severity**: LOW
- **Language**: all
- **Category**: style
- Using raw primitives (strings, ints, floats) where a domain type would be more expressive leads to models where invalid states are representable. Represent domain concepts with dedicated types that encode their constraints and prevent mixing up arguments with similar base types.
- **Bad:**
```kotlin
fun transfer(amount: Double, fromAccount: String, toAccount: String)
// nothing stops the caller from swapping fromAccount and toAccount
```
- **Good:**
```kotlin
fun transfer(amount: Money, from: AccountId, to: AccountId)
// wrong-argument-order bugs caught at compile time
```

### CP015 — Avoid Feature Envy
- **Severity**: LOW
- **Language**: all
- **Category**: architecture
- A method that calls many getters on another object to compute its result "envies" that object's data — the logic probably belongs on that other class. Move the method closer to the data it operates on, or introduce a richer domain object that encapsulates the behaviour.
- **Bad:**
```python
class InvoiceFormatter:
    def calculate_total(self, order) -> float:
        return sum(line.qty * line.unit_price for line in order.lines) * (1 - order.discount_rate)
        # operates entirely on Order's internals — belongs on Order
```
- **Good:**
```python
class Order:
    def total_due(self) -> float:
        return sum(line.subtotal for line in self.lines) * (1 - self.discount_rate)
```
