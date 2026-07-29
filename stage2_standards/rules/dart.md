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
- **Category**: performance
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

### DA008 — No BuildContext Use After an Async Gap Without a mounted Check
- **Severity**: HIGH
- **Language**: dart
- **Category**: error_handling
- After an `await`, the widget may have been disposed (user navigated away) before execution resumes. Using `context`, calling `setState()`, or reading `Theme.of(context)`/`Navigator.of(context)` after an `await` without first checking `mounted` risks a thrown exception or a rebuild on a defunct widget.
- **Bad:**
```dart
Future<void> _submit() async {
    await api.postOrder(order);
    Navigator.of(context).pop(); // context may belong to a disposed widget
}
```
- **Good:**
```dart
Future<void> _submit() async {
    await api.postOrder(order);
    if (!mounted) return;
    Navigator.of(context).pop();
}
```

### DA009 — Provide Keys for Widgets Built From a Dynamic List
- **Severity**: MEDIUM
- **Language**: dart
- **Category**: error_handling
- Widgets generated from a `List.map`, `List.generate`, or a builder callback (`ListView.builder`, `Column(children: ...)`) must carry a stable `key` when the underlying list can reorder, insert, or remove items. Without a key, Flutter matches widgets by position, mixing up controller/animation/focus state when the list changes.
- **Bad:**
```dart
Column(
  children: items.map((item) => ItemTile(item: item)).toList(),
)
```
- **Good:**
```dart
Column(
  children: items.map((item) => ItemTile(key: ValueKey(item.id), item: item)).toList(),
)
```

### DA010 — Use Lazy Builders for Large or Unbounded Lists
- **Severity**: MEDIUM
- **Language**: dart
- **Category**: performance
- A list of unknown or unbounded size must be rendered with `ListView.builder`/`GridView.builder` (which build items lazily, on demand) rather than eagerly materialising every child up front via `.map(...).toList()` inside a non-lazy `ListView`/`Column`. Eager construction builds and lays out every off-screen item immediately, which does not scale.
- **Bad:**
```dart
ListView(
  children: products.map((p) => ProductCard(product: p)).toList(),
)
```
- **Good:**
```dart
ListView.builder(
  itemCount: products.length,
  itemBuilder: (context, i) => ProductCard(product: products[i]),
)
```
