# Dart / Flutter Standards

Rules specific to **Dart** (Flutter) code in this project.

Severity levels: CRITICAL > HIGH > MEDIUM > LOW > INFO

---

## Dart / Flutter Rules

### DA001 — Dispose Controllers and Subscriptions
- **Severity**: HIGH
- **Language**: dart
- **Category**: error_handling
- `TextEditingController`, `AnimationController`, `StreamSubscription`, and similar objects must be disposed in `dispose()` to prevent memory leaks.
- **Bad:**
```dart
class _MyWidgetState extends State<MyWidget> {
    final controller = TextEditingController();
    // No dispose() — controller leaks
}
```
- **Good:**
```dart
class _MyWidgetState extends State<MyWidget> {
    final controller = TextEditingController();

    @override
    void dispose() {
        controller.dispose();
        super.dispose();
    }
}
```

### DA002 — No setState() in initState()
- **Severity**: HIGH
- **Language**: dart
- **Category**: error_handling
- Calling `setState()` inside `initState()` triggers an extra rebuild before the first frame is painted, wasting resources. Move async work to `didChangeDependencies` or schedule with `addPostFrameCallback`.
- **Bad:**
```dart
@override
void initState() {
    super.initState();
    setState(() { data = fetch(); }); // wrong
}
```
- **Good:**
```dart
@override
void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
        setState(() { data = fetch(); });
    });
}
```

### DA003 — Keep build() Pure (No Side Effects)
- **Severity**: HIGH
- **Language**: dart
- **Category**: architecture
- The `build()` method may be called many times per second. It must be a pure function of state and props — no I/O, no `setState()`, no `print()` calls.
- **Bad:**
```dart
@override
Widget build(BuildContext context) {
    fetchData(); // side effect in build
    return Text(data);
}
```
- **Good:**
```dart
@override
void initState() {
    super.initState();
    fetchData();
}

@override
Widget build(BuildContext context) => Text(data);
```

### DA004 — Use const Constructors Where Possible
- **Severity**: LOW
- **Language**: dart
- **Category**: style
- Mark widget constructors `const` when all fields are known at compile time. Flutter can skip rebuilding `const` subtrees entirely.
- **Bad:**
```dart
Text('Hello')
SizedBox(height: 16)
```
- **Good:**
```dart
const Text('Hello')
const SizedBox(height: 16)
```

### DA005 — No Business Logic in Widgets (BLoC/Riverpod Pattern)
- **Severity**: MEDIUM
- **Language**: dart
- **Category**: architecture
- Widgets should only build UI and dispatch events. All business logic (API calls, state computation, validation) must live in a BLoC, Cubit, Provider, or ViewModel.
- **Bad:**
```dart
class CartWidget extends StatelessWidget {
    void _checkout() {
        final total = cart.items.fold(0, (s, i) => s + i.price);
        api.post('/orders', {'total': total}); // business logic in widget
    }
}
```
- **Good:**
```dart
class CartWidget extends StatelessWidget {
    void _checkout(BuildContext context) {
        context.read<CartBloc>().add(CheckoutEvent());
    }
}
```

### DA006 — No Hardcoded UI Strings
- **Severity**: MEDIUM
- **Language**: dart
- **Category**: style
- Strings shown in the UI must come from a localisation file (ARB/intl). Hardcoded strings make the app impossible to localise.
- **Bad:**
```dart
Text('Welcome back!')
```
- **Good:**
```dart
Text(AppLocalizations.of(context)!.welcomeBack)
```

### DA007 — Avoid Rebuilding Expensive Widgets
- **Severity**: MEDIUM
- **Language**: dart
- **Category**: complexity
- Extract expensive subtrees into separate `StatelessWidget` or `StatefulWidget` classes so Flutter can short-circuit their rebuild. Do not inline them in large `build()` methods.
- **Bad:**
```dart
// All in one giant build() — everything rebuilds on every setState
Widget build(BuildContext context) {
    return Column(children: [
        HeavyChart(data: data),   // rebuilds even if data unchanged
        SimpleText(label),
    ]);
}
```
- **Good:**
```dart
// HeavyChart is a separate widget; Flutter compares it by type+key
return Column(children: [
    const HeavyChart(),
    SimpleText(label),
]);
```
