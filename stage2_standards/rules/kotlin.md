# Kotlin / Android Standards

Rules specific to **Kotlin** (Android) code in this project.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## Kotlin Rules

### KT001 — No Non-Null Assertion (!!)
- **Severity**: HIGH
- **Language**: kotlin
- **Category**: error_handling
- The `!!` operator throws `NullPointerException` at runtime if the value is null. Use `?.let`, `?:`, safe casts, or `requireNotNull()` instead.
- **Bad:**
```kotlin
val length = user!!.name!!.length
```
- **Good:**
```kotlin
val length = user?.name?.length ?: 0
```

### KT002 — Use Data Classes for Plain Data Holders
- **Severity**: LOW
- **Language**: kotlin
- **Category**: style
- Classes that only hold data with no behaviour should be `data class`. This gives free `equals`, `hashCode`, `toString`, and `copy`.
- **Bad:**
```kotlin
class Point(val x: Int, val y: Int)
```
- **Good:**
```kotlin
data class Point(val x: Int, val y: Int)
```

### KT003 — Handle Exceptions in launch {} Blocks
- **Severity**: HIGH
- **Language**: kotlin
- **Category**: error_handling
- Uncaught exceptions inside `launch {}` crash the coroutine silently. Use a `CoroutineExceptionHandler` or wrap the body in `try/catch`.
- **Bad:**
```kotlin
viewModelScope.launch {
    fetchData() // exception here crashes silently
}
```
- **Good:**
```kotlin
viewModelScope.launch {
    try {
        fetchData()
    } catch (e: IOException) {
        _uiState.value = UiState.Error(e.message)
    }
}
```

### KT004 — ViewModel Must Not Hold Activity References
- **Severity**: CRITICAL
- **Language**: kotlin
- **Category**: architecture
- Holding a reference to an `Activity`, `Fragment`, or `View` inside a `ViewModel` causes memory leaks because the ViewModel outlives the UI component.
- **Bad:**
```kotlin
class MyViewModel(val activity: MainActivity) : ViewModel()
```
- **Good:**
```kotlin
class MyViewModel(app: Application) : AndroidViewModel(app)
// Use Application context only, or pass data via LiveData/StateFlow.
```

### KT005 — Use Sealed Classes for State
- **Severity**: MEDIUM
- **Language**: kotlin
- **Category**: architecture
- Use `sealed class` (or `sealed interface`) to represent UI states and results. This forces exhaustive `when` expressions at compile time.
- **Bad:**
```kotlin
var state: String = "loading" // "loading" | "success" | "error"
```
- **Good:**
```kotlin
sealed class UiState {
    object Loading : UiState()
    data class Success(val data: List<Item>) : UiState()
    data class Error(val message: String) : UiState()
}
```

### KT006 — Prefer StateFlow/SharedFlow Over LiveData in New Code
- **Severity**: INFO
- **Language**: kotlin
- **Category**: style
- New ViewModels should use `StateFlow`/`SharedFlow` (coroutine-native) over `LiveData` (lifecycle-library). `LiveData` is still acceptable in legacy code.
- **Bad:**
```kotlin
private val state = MutableLiveData<UiState>()
```
- **Good:**
```kotlin
private val _uiState = MutableStateFlow<UiState>(UiState.Loading)
val uiState: StateFlow<UiState> = _uiState.asStateFlow()
```

### KT007 — No Hardcoded UI Strings
- **Severity**: MEDIUM
- **Language**: kotlin
- **Category**: style
- String literals passed directly to `TextView.text`, `setText(...)`, `Toast.makeText(...)`, or a Composable `Text(...)` must come from `strings.xml`, not be hardcoded — required for localisation. Log messages, analytics event names, and exception text are not in scope.
- **Bad:**
```kotlin
textView.text = "Welcome back!"
```
- **Good:**
```kotlin
textView.text = getString(R.string.welcome_back)
```

### KT008 — Use Extension Functions Over Utility Classes
- **Severity**: INFO
- **Language**: kotlin
- **Category**: style
- Prefer an extension function over a static utility `object` that wraps a single, unrelated helper method — the anti-pattern is single-method wrapper objects, not utility objects in general. A cohesive `object` grouping several related operations is fine as-is.
- **Bad:**
```kotlin
object StringUtils {
    fun capitalise(s: String) = s.replaceFirstChar { it.uppercase() }
}
StringUtils.capitalise(name)
```
- **Good:**
```kotlin
fun String.capitalise() = replaceFirstChar { it.uppercase() }
name.capitalise()
```
