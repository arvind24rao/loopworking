# Loop MVP — Flutter Frontend Handover Document

**Last updated:** 2025-09-13 23:53 SGT  
**Owner:** AR (handover-ready)  
**Project path (local):** `/Users/arvindrao/loop`

---


## 1) Executive Summary

This repository contains a barebones Flutter frontend for an AI-assisted messenger MVP. It currently implements:

- **Environment setup** (fresh Flutter app, http dependency)
- **Config via `--dart-define`** for `API_BASE_URL`
- **Health pill** (polling `GET /health`)
- **Identity toggle (User A/B)** sending `X-User-Id` on requests
- **Messages list** rendering `GET /threads/{threadId}/messages`

_Planned (not yet implemented in UI):_
- Composer (text input) + **Send**: `POST /messages/inbox`
- **Publish** action per message: `POST /messages/publish`

---

## 2) Environment & Dependencies

- Flutter: recent stable (tested on iOS Simulator, macOS)
- Platforms enabled: **android**, **ios**
- Dependency added:
  ```bash
  flutter pub add http
  ```

### Create & run (recap)
```bash
# created in: /Users/arvindrao/loop
flutter create --org com.example --project-name loop_messenger --platforms=android,ios loop
cd /Users/arvindrao/loop

# run (iOS simulator example)
open -a Simulator
flutter devices
flutter run -d "iPhone 16 Plus"
```

### Pointing at backend
```bash
# Override base URL at launch
flutter run -d "iPhone 16 Plus" --dart-define=API_BASE_URL=http://localhost:8080
```
- Default (`lib/core/app_config.dart`) is `http://localhost:8080`.
- **iOS ATS**: If targeting **non-localhost HTTP**, add a domain-specific ATS exception in `ios/Runner/Info.plist` (dev only).

---

## 3) Backend Contract (currently used)

1. `GET /health` → returns JSON with:
   - `status` (e.g., "ok" | "degraded")
   - `rest_ok` (bool), `db_ok` (bool)
   - optional `latency_ms` (number)

2. `GET /threads/{thread_id}/messages?order=created_at.desc&limit=50`
   - Returns an array of messages; UI reverses to **oldest → newest** for chat display.

3. (Planned) `POST /messages/inbox`
   - Body: `{ thread_id, content_plain }`

4. (Planned) `POST /messages/publish`
   - Body: `{ inbox_message_id, summary_override? }`

**Identity header (used on authenticated calls):**
```
X-User-Id: <current-user-uuid>
```

---

## 4) Dev Constants

Defined in `lib/core/app_config.dart`:
```dart
static const String threadId = 'b01164e6-c719-4fb1-b2d0-85755e7ebf38';
static const String userA   = 'b8d99c3c-0d3a-4773-a324-a6bc60dee64e';
static const String userB   = '0dd8b495-6a25-440d-a6e4-d8b7a77bc688';
```
---

## 5) Project Structure (relevant parts)

```
lib/
  core/
    app_config.dart
    api_client.dart
    models/
      health_status.dart
      message.dart
  features/
    health/
      health_badge.dart
    identity/
      user_toggle.dart
    messages/
      message_list.dart
  main.dart
```
---

## 6) Build/Run Recipes

**iOS (Simulator):**
```bash
open -a Simulator
flutter run -d "iPhone 16 Plus" --dart-define=API_BASE_URL=http://localhost:8080
```

**Android (when Android SDK installed):**
```bash
flutter devices
flutter run -d android --dart-define=API_BASE_URL=http://10.0.2.2:8080
```

**Common fixes:**
```bash
# CocoaPods (if needed)
sudo gem install cocoapods
cd ios && pod install && cd ..

# After plist or dependency changes
flutter clean
flutter pub get
```
---

### 7.1 `lib/core/app_config.dart`
```dart
// lib/core/app_config.dart
// Centralised dev config.
class AppConfig {
  static const String baseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://localhost:8080',
  );

  // Fixed MVP thread & test users.
  static const String threadId = 'b01164e6-c719-4fb1-b2d0-85755e7ebf38';
  static const String userA = 'b8d99c3c-0d3a-4773-a324-a6bc60dee64e';
  static const String userB = '0dd8b495-6a25-440d-a6e4-d8b7a77bc688';
}
```

### 7.2 `lib/core/models/health_status.dart`
```dart
// lib/core/models/health_status.dart
class HealthStatus {
  final String status;   // "ok" | "degraded" | etc.
  final bool restOk;
  final bool dbOk;
  final int? latencyMs;  // optional server-provided latency

  const HealthStatus({
    required this.status,
    required this.restOk,
    required this.dbOk,
    this.latencyMs,
  });

  bool get isConnected =>
      (status.toLowerCase() == 'ok' || status.toLowerCase() == 'healthy') &&
      restOk &&
      dbOk;

  bool get isDegraded =>
      !isConnected && (restOk || dbOk || status.toLowerCase() == 'degraded');

  factory HealthStatus.fromJson(Map<String, dynamic> json) {
    return HealthStatus(
      status: (json['status'] ?? '').toString(),
      restOk: json['rest_ok'] == true,
      dbOk: json['db_ok'] == true,
      latencyMs: json['latency_ms'] is num ? (json['latency_ms'] as num).toInt() : null,
    );
  }

  HealthStatus copyWith({int? latencyMs}) => HealthStatus(
        status: status,
        restOk: restOk,
        dbOk: dbOk,
        latencyMs: latencyMs ?? this.latencyMs,
      );
}
```

### 7.3 `lib/core/models/message.dart`
```dart
// lib/core/models/message.dart

/// Minimal shape based on your backend guide.
class Message {
  final String id;
  final String threadId;
  final String? authorId; // null or absent when system/AI? We'll handle gracefully.
  final DateTime createdAt;
  final String textRaw;
  final bool isAI; // best-effort flag if backend includes it

  Message({
    required this.id,
    required this.threadId,
    required this.authorId,
    required this.createdAt,
    required this.textRaw,
    required this.isAI,
  });

  /// Strip any "cipher:" prefix before displaying to users.
  String get displayText {
    const prefix = 'cipher:';
    final t = textRaw;
    if (t.startsWith(prefix)) return t.substring(prefix.length).trimLeft();
    return t;
  }

  factory Message.fromJson(Map<String, dynamic> json) {
    // Accept multiple possible keys for content/body to be robust.
    final raw = (json['content_plain'] ??
            json['text'] ??
            json['content'] ??
            json['body'] ??
            '')
        .toString();

    // Handle typical timestamp keys.
    final ts = (json['created_at'] ?? json['inserted_at'] ?? json['ts'] ?? '').toString();
    final created = DateTime.tryParse(ts) ?? DateTime.now();

    return Message(
      id: (json['id'] ?? '').toString(),
      threadId: (json['thread_id'] ?? '').toString(),
      authorId: json['author_id']?.toString(),
      createdAt: created,
      textRaw: raw,
      isAI: (json['is_ai'] == true) ||
          (json['author_type']?.toString().toLowerCase() == 'ai') ||
          (json['role']?.toString().toLowerCase() == 'assistant'),
    );
  }
}
```

### 7.4 `lib/core/api_client.dart`
```dart
// lib/core/api_client.dart
import 'dart:convert';
import 'package:http/http.dart' as http;

import 'app_config.dart';
import 'models/health_status.dart';
import 'models/message.dart';

class ApiClient {
  ApiClient({
    http.Client? httpClient,
    String? baseUrl,
  })  : _http = httpClient ?? http.Client(),
        baseUrl = (baseUrl ?? AppConfig.baseUrl).replaceAll(RegExp(r'/*$'), '');

  final http.Client _http;
  final String baseUrl;

  /// Identity toggle (User A/B) sets this.
  String? userId;

  Map<String, String> _headers({bool withIdentity = false}) {
    final headers = <String, String>{
      'Accept': 'application/json',
      'Content-Type': 'application/json',
    };
    if (withIdentity && userId != null && userId!.trim().isNotEmpty) {
      headers['X-User-Id'] = userId!.trim();
    }
    return headers;
  }

  Uri _u(String path, [Map<String, String>? query]) {
    final p = path.startsWith('/') ? path : '/$path';
    return Uri.parse('$baseUrl$p').replace(queryParameters: query);
  }

  /// GET /health — returns HealthStatus
  Future<HealthStatus> getHealth() async {
    final started = DateTime.now().millisecondsSinceEpoch;
    final res = await _http.get(_u('/health'), headers: _headers());
    final ended = DateTime.now().millisecondsSinceEpoch;

    if (res.statusCode < 200 || res.statusCode >= 300) {
      throw HttpException(
        code: res.statusCode,
        message: 'Health check failed',
        body: res.body,
      );
    }

    final body = res.body.isEmpty ? <String, dynamic>{} : json.decode(res.body) as Map<String, dynamic>;
    final parsed = HealthStatus.fromJson(body);

    // If server didn’t return latency, use client-measured latency.
    final measured = ended - started;
    return parsed.latencyMs == null ? parsed.copyWith(latencyMs: measured) : parsed;
  }

  /// GET /threads/{threadId}/messages
  Future<List<Message>> listMessages({
    required String threadId,
    int limit = 50,
  }) async {
    final res = await _http.get(
      _u('/threads/$threadId/messages', {
        'order': 'created_at.desc',
        'limit': '$limit',
      }),
      headers: _headers(withIdentity: true),
    );

    if (res.statusCode < 200 || res.statusCode >= 300) {
      throw HttpException(
        code: res.statusCode,
        message: 'Failed to fetch messages',
        body: res.body,
      );
    }

    final data = (json.decode(res.body) as List).cast<dynamic>();
    final items = data.map((e) => Message.fromJson((e as Map).cast<String, dynamic>())).toList();

    // Reverse to oldest->newest for natural chat display.
    return items.reversed.toList(growable: false);
  }

  void close() => _http.close();
}

class HttpException implements Exception {
  final int code;
  final String message;
  final String? body;

  HttpException({required this.code, required this.message, this.body});

  @override
  String toString() => 'HttpException($code): $message${body != null ? ' — $body' : ''}';
}
```

### 7.5 `lib/features/health/health_badge.dart`
```dart
// lib/features/health/health_badge.dart
import 'dart:async';
import 'package:flutter/material.dart';
import '../../core/api_client.dart';
import '../../core/models/health_status.dart';

class HealthBadge extends StatefulWidget {
  const HealthBadge({super.key, required this.api});
  final ApiClient api;

  @override
  State<HealthBadge> createState() => _HealthBadgeState();
}

class _HealthBadgeState extends State<HealthBadge> {
  HealthStatus? _status;
  String? _error;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _load();
    _timer = Timer.periodic(const Duration(seconds: 10), (_) => _load());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final s = await widget.api.getHealth();
      if (!mounted) return;
      setState(() {
        _status = s;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _status = null;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    if (_error != null) {
      return Tooltip(
        message: _error!,
        child: _pill(theme, 'Offline', theme.colorScheme.error.withOpacity(0.15), theme.colorScheme.error),
      );
    }

    if (_status == null) {
      return _pill(theme, 'Checking…', theme.colorScheme.surfaceVariant, theme.colorScheme.onSurfaceVariant);
    }

    final s = _status!;
    if (s.isConnected) {
      return Tooltip(
        message: 'REST OK • DB OK • ${s.latencyMs ?? '—'}ms',
        child: _pill(theme, 'Connected', theme.colorScheme.primary.withOpacity(0.12), theme.colorScheme.primary),
      );
    }

    if (s.isDegraded) {
      return Tooltip(
        message: 'Status: ${s.status} • REST: ${s.restOk} • DB: ${s.dbOk} • ${s.latencyMs ?? '—'}ms',
        child: _pill(theme, 'Degraded', theme.colorScheme.tertiary.withOpacity(0.15), theme.colorScheme.tertiary),
      );
    }

    return Tooltip(
      message: 'Status: ${s.status} • REST: ${s.restOk} • DB: ${s.dbOk}',
      child: _pill(theme, 'Offline', theme.colorScheme.error.withOpacity(0.15), theme.colorScheme.error),
    );
  }

  Widget _pill(ThemeData theme, String text, Color bg, Color fg) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: fg.withOpacity(0.3)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.circle, size: 8, color: fg),
          const SizedBox(width: 6),
          Text(text, style: theme.textTheme.labelMedium?.copyWith(color: fg)),
        ],
      ),
    );
  }
}
```

### 7.6 `lib/features/identity/user_toggle.dart`
```dart
// lib/features/identity/user_toggle.dart
import 'package:flutter/material.dart';

class UserOption {
  final String id;
  final String label;
  const UserOption(this.id, this.label);
}

class UserToggle extends StatelessWidget {
  const UserToggle({
    super.key,
    required this.options,
    required this.currentId,
    required this.onChanged,
  });

  final List<UserOption> options;
  final String currentId;
  final ValueChanged<UserOption> onChanged;

  @override
  Widget build(BuildContext context) {
    // Compact SegmentedButton
    return _Segmented(
      options: options,
      currentId: currentId,
      onChanged: onChanged,
    );
  }
}

class _Segmented extends StatelessWidget {
  const _Segmented({
    required this.options,
    required this.currentId,
    required this.onChanged,
  });

  final List<UserOption> options;
  final String currentId;
  final ValueChanged<UserOption> onChanged;

  @override
  Widget build(BuildContext context) {
    final items = <ButtonSegment<String>>[
      for (final o in options) ButtonSegment(value: o.id, label: Text(o.label)),
    ];

    return SegmentedButton<String>(
      segments: items,
      selected: {currentId},
      style: ButtonStyle(
        visualDensity: VisualDensity.compact,
        padding: WidgetStatePropertyAll(
          const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        ),
      ),
      onSelectionChanged: (sel) {
        final id = sel.first;
        final found = options.firstWhere((o) => o.id == id);
        onChanged(found);
      },
    );
  }
}
```

### 7.7 `lib/features/messages/message_list.dart`
```dart
// lib/features/messages/message_list.dart
import 'dart:async';
import 'package:flutter/material.dart';
import '../../core/api_client.dart';
import '../../core/models/message.dart';
import '../../core/app_config.dart';

class MessageList extends StatefulWidget {
  const MessageList({
    super.key,
    required this.api,
    required this.currentUserId,
  });

  final ApiClient api;
  final String currentUserId;

  @override
  State<MessageList> createState() => _MessageListState();
}

class _MessageListState extends State<MessageList> {
  List<Message> _messages = const [];
  String? _error;
  bool _loading = false;
  Timer? _poller;

  @override
  void initState() {
    super.initState();
    _load();
    // Light polling for MVP.
    _poller = Timer.periodic(const Duration(seconds: 8), (_) => _load());
  }

  @override
  void didUpdateWidget(covariant MessageList oldWidget) {
    super.didUpdateWidget(oldWidget);
    // If user changes, reload to reflect visibility differences.
    if (oldWidget.currentUserId != widget.currentUserId) {
      _load();
    }
  }

  @override
  void dispose() {
    _poller?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = _messages.isEmpty; // skeleton only on first load
      _error = null;
    });
    try {
      final items = await widget.api.listMessages(threadId: AppConfig.threadId);
      if (!mounted) return;
      setState(() {
        _messages = items;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const _Skeleton();
    }
    if (_error != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('Could not load messages', style: Theme.of(context).textTheme.titleSmall),
            const SizedBox(height: 8),
            Text(_error!, style: Theme.of(context).textTheme.bodySmall),
            const SizedBox(height: 12),
            FilledButton.tonal(onPressed: _load, child: const Text('Retry')),
          ],
        ),
      );
    }

    if (_messages.isEmpty) {
      return RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          children: [
            const SizedBox(height: 120),
            Center(
              child: Text(
                'No messages yet',
                style: Theme.of(context).textTheme.bodyLarge,
              ),
            ),
            const SizedBox(height: 8),
            Center(
              child: Text(
                'Send a message to start the conversation',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
          ],
        ),
      );
    }

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView.builder(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        itemCount: _messages.length,
        itemBuilder: (context, index) {
          final m = _messages[index];
          final isMe = (m.authorId != null && m.authorId == widget.currentUserId);
          final isAI = m.isAI;

          final bg = isAI
              ? Theme.of(context).colorScheme.tertiaryContainer
              : isMe
                  ? Theme.of(context).colorScheme.primaryContainer
                  : Theme.of(context).colorScheme.surfaceVariant;

          final fg = isAI
              ? Theme.of(context).colorScheme.onTertiaryContainer
              : isMe
                  ? Theme.of(context).colorScheme.onPrimaryContainer
                  : Theme.of(context).colorScheme.onSurfaceVariant;

          final align = isMe ? CrossAxisAlignment.end : CrossAxisAlignment.start;
          final bubbleAlign = isMe ? Alignment.centerRight : Alignment.centerLeft;

          return Column(
            crossAxisAlignment: align,
            children: [
              Align(
                alignment: bubbleAlign,
                child: ConstrainedBox(
                  constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.78),
                  child: Container(
                    margin: const EdgeInsets.symmetric(vertical: 6),
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                    decoration: BoxDecoration(
                      color: bg,
                      borderRadius: BorderRadius.circular(16).copyWith(
                        topLeft: isMe ? const Radius.circular(16) : Radius.zero,
                        topRight: isMe ? Radius.zero : const Radius.circular(16),
                      ),
                      border: Border.all(
                        color: Theme.of(context).colorScheme.outlineVariant,
                        width: 0.6,
                      ),
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        if (isAI)
                          Padding(
                            padding: const EdgeInsets.only(bottom: 4.0),
                            child: Text(
                              'Loop Assistant',
                              style: Theme.of(context).textTheme.labelSmall?.copyWith(color: fg.withOpacity(0.9)),
                            ),
                          ),
                        Text(
                          m.displayText,
                          style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: fg),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
              Padding(
                padding: const EdgeInsets.only(top: 2.0, left: 8, right: 8),
                child: Text(
                  _formatTime(m.createdAt),
                  style: Theme.of(context).textTheme.labelSmall?.copyWith(
                        color: Theme.of(context).colorScheme.outline,
                      ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  String _formatTime(DateTime dt) {
    final hh = dt.hour.toString().padLeft(2, '0');
    final mm = dt.minute.toString().padLeft(2, '0');
    return '$hh:$mm';
  }
}

class _Skeleton extends StatelessWidget {
  const _Skeleton();

  @override
  Widget build(BuildContext context) {
    final base = Theme.of(context).colorScheme.surfaceVariant;
    return ListView.builder(
      itemCount: 8,
      padding: const EdgeInsets.all(12),
      itemBuilder: (_, i) => Container(
        margin: const EdgeInsets.symmetric(vertical: 8),
        height: 56,
        decoration: BoxDecoration(
          color: base.withOpacity(0.6),
          borderRadius: BorderRadius.circular(12),
        ),
      ),
    );
  }
}
```

### 7.8 `lib/main.dart`
```dart
// lib/main.dart
import 'package:flutter/material.dart';
import 'core/api_client.dart';
import 'core/app_config.dart';
import 'features/health/health_badge.dart';
import 'features/identity/user_toggle.dart';
import 'features/messages/message_list.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const LoopApp());
}

class LoopApp extends StatefulWidget {
  const LoopApp({super.key});

  @override
  State<LoopApp> createState() => _LoopAppState();
}

class _LoopAppState extends State<LoopApp> {
  late final ApiClient _api;

  // In-memory identity
  late String _currentUserId;
  late String _currentUserLabel;

  @override
  void initState() {
    super.initState();
    _api = ApiClient();

    _currentUserId = AppConfig.userA;
    _currentUserLabel = 'User A';
    _api.userId = _currentUserId;
  }

  @override
  void dispose() {
    _api.close();
    super.dispose();
  }

  void _onUserChanged(UserOption u) {
    setState(() {
      _currentUserId = u.id;
      _currentUserLabel = u.label;
      _api.userId = _currentUserId;
    });
  }

  @override
  Widget build(BuildContext context) {
    final userOptions = [
      UserOption(AppConfig.userA, 'User A'),
      UserOption(AppConfig.userB, 'User B'),
    ];

    return MaterialApp(
      title: 'Loop — MVP',
      theme: ThemeData(
        colorSchemeSeed: Colors.blue,
        useMaterial3: true,
      ),
      home: Scaffold(
        appBar: AppBar(
          title: const Text('Loop — MVP'),
          actions: [
            Padding(
              padding: const EdgeInsets.only(right: 8.0),
              child: Center(
                child: UserToggle(
                  options: userOptions,
                  currentId: _currentUserId,
                  onChanged: _onUserChanged,
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(right: 10.0),
              child: Center(
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                  decoration: BoxDecoration(
                    color: Theme.of(context).colorScheme.surfaceVariant,
                    borderRadius: BorderRadius.circular(999),
                    border: Border.all(
                      color: Theme.of(context).colorScheme.outlineVariant,
                    ),
                  ),
                  child: Text(
                    _currentUserLabel,
                    style: Theme.of(context)
                        .textTheme
                        .labelMedium
                        ?.copyWith(color: Theme.of(context).colorScheme.onSurfaceVariant),
                  ),
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12.0),
              child: Center(child: HealthBadge(api: _api)),
            ),
          ],
        ),
        body: MessageList(api: _api, currentUserId: _currentUserId),
      ),
    );
  }
}
```

---

## 8) Next Steps (recommended)

1. **Composer UI** (bottom text field) → `POST /messages/inbox`
   - Add optimistic bubble "sending…", then replace with server message on success.
   - Error path: show "Failed. Retry".

2. **Publish action** per message → `POST /messages/publish`
   - Disable while in-flight; on success, refetch list so AI message appears.

3. **Error banners & validation**
   - Map 400/403/404/500 to friendly messages (some already in place).

4. **Polling → Realtime**
   - Replace 8–10s polling with a websocket/stream when backend supports it.

---

## 9) Test Flow (manual)

1. Launch with working backend:
   ```bash
   flutter run -d "iPhone 16 Plus" --dart-define=API_BASE_URL=http://localhost:8080
   ```
2. Confirm health pill shows **Connected**.
3. Toggle **User A/User B** and ensure calls include `X-User-Id`.
4. Messages screen should show the fixed thread’s messages (or "No messages yet").

---

## 10) Changelog (high-level)

- **Step 0–1:** Created fresh Flutter app; added `http`; connected `/health` badge.
- **Step 2:** Added identity toggle (User A/B) and wired `X-User-Id` in `ApiClient`.
- **Step 3:** Implemented messages list (GET `/threads/{id}/messages`) with pull-to-refresh + light polling.

---

If you need anything else packaged (e.g., a zip of current `lib/`), say the word.
