# Canvas Tracker Architecture & Future Roadmap

This document outlines the architecture of Canvas Tracker, details our privacy and security design principles, and records architectural decisions for running the application in-browser via **stlite** with a **Cloudflare Workers** stateless CORS proxy.

---

## 1. Core Principles: Privacy-First & Zero-Trust Cloud

Canvas Student Tracker handles sensitive educational data and parent credentials:
- **Canvas API Bearer Tokens**: Grant full programmatic access to student records, grades, submissions, and messages.
- **Student PII & Academic Records**: Protected by privacy standards (e.g. FERPA in the US). Student names, assignments, feedback, and grades must never be leaked to third-party multi-tenant servers or unvetted cloud hosts.

### Why Streamlit Community Cloud Was Rejected
1. **Shared Multi-Tenant Server**: Secrets and memory live on third-party servers outside parent control.
2. **Token Security**: Storing personal parent bearer tokens in a hosted cloud environment creates unnecessary liability.
3. **Session Persistence**: Free community cloud apps sleep, reset state, and lack private client-side storage without external databases.

---

## 2. Current Architecture (Desktop / Local Python)

```
┌────────────────────────────────────────────────────────┐
│                   Desktop / Local Machine              │
│                                                        │
│  ┌──────────────────┐         ┌─────────────────────┐  │
│  │  Streamlit App   │ ◄─────► │ canvas_gradebook.py │  │
│  │ canvas_tracker.py│         │  (grading engine)   │  │
│  └────────▲─────────┘         └─────────────────────┘  │
│           │                                            │
│           │                  ┌──────────────────────┐  │
│           ├────────────────► │   canvas_notes.py    │  │
│           │                  │ (Pluggable Backend)  │  │
│           │                  └──────────┬───────────┘  │
│           │                             │              │
│           │                   ┌─────────┴─────────┐    │
│           │                   ▼                   ▼    │
│           │          [canvas_notes.json]   (Google Sheet│
│           │            (Local default)     Web App Sync)│
└───────────┼───────────────────────────────────────┼────┘
            │ HTTPS API                             │ HTTPS Webhook
            ▼                                       ▼
 ┌──────────────────────┐               ┌──────────────────────┐
 │      Canvas LMS      │               │   User's Personal    │
 │ (AACPS Instructure)  │               │     Google Sheet     │
 └──────────────────────┘               └──────────────────────┘
```

### Components
1. **`canvas_tracker.py`**:
   - Streamlit dashboard UI and CLI runner.
   - Data polling, background auto-refresh via `@st.fragment`, active-hours scheduling, and tab focus manager.
   - Renders interactive Detailed Gradebook and Incomplete Assignments data tables.
2. **`canvas_gradebook.py`**:
   - Grade calculation engine reproducing Canvas weighted and unweighted grading rules.
   - Computes overall grades, category health, grade drag/boost impacts, and interactive what-if grade simulations.
3. **`canvas_notes.py`**:
   - Unified notes persistence layer.
   - Default: `LocalNotesBackend` writing to git-ignored `canvas_notes.json` in the project root.
   - Optional: `GoogleSheetNotesBackend` syncing notes to a personal Google Sheet via a lightweight Google Apps Script Web App.
   - Provides 1-click test & connection validation and local-to-cloud export.

---

## 3. Future Roadmap: In-Browser Client (`stlite` + Cloudflare Workers)

To allow Canvas Tracker to run as a client-side web application on phones and tablets (PWA) with zero server hosting costs and maximum privacy, the planned target architecture uses **stlite**.

### What is `stlite`?
[`stlite`](https://github.com/whitphx/stlite) runs Streamlit entirely inside the browser using Pyodide (Python compiled to WebAssembly). The Python interpreter, Pandas, and application logic execute on the user's device (phone, iPad, or laptop).

### The Challenge: Browser CORS
When running Python in native desktop mode, Python's `requests` library communicates directly with Canvas API (`https://aacps.instructure.com/api/v1/...`). In native desktop Python, CORS (Cross-Origin Resource Sharing) does not apply.

However, inside a browser (`stlite` via Pyodide), HTTP requests are mediated by the browser's `fetch()` API. Because Instructure Canvas API does not return `Access-Control-Allow-Origin: *` headers to arbitrary web pages, direct requests from the browser to Canvas will be blocked by the browser's CORS policy.

### The Solution: Cloudflare Workers Stateless CORS Proxy

```
┌──────────────────────────────────────────────────────────────┐
│                     User's Web Browser                       │
│                     (Phone / iPad / PC)                      │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │               stlite (Pyodide in WASM)                 │  │
│  │                                                        │  │
│  │  - canvas_tracker.py                                   │  │
│  │  - canvas_gradebook.py                                 │  │
│  │  - canvas_notes.py (uses browser window.localStorage)  │  │
│  └───────────────────────────┬────────────────────────────┘  │
└──────────────────────────────┼───────────────────────────────┘
                               │ HTTPS fetch()
                               ▼
            ┌──────────────────────────────────────┐
            │       Cloudflare Worker Proxy        │
            │    (Stateless, Personal Account)     │
            │                                      │
            │  - Forwards Authorization header     │
            │  - Appends CORS response headers     │
            │  - Zero storage / No logs / No state │
            └──────────────────┬───────────────────┘
                               │ HTTPS API
                               ▼
                    ┌──────────────────────┐
                    │      Canvas LMS      │
                    │ (AACPS Instructure)  │
                    └──────────────────────┘
```

#### Why Cloudflare Workers?
1. **Stateless Execution**:
   - Cloudflare Workers run on Cloudflare's edge without disk storage, databases, or local caching by default.
   - Requests and responses pass through in memory. No student data or parent tokens are logged or retained.
2. **Account Specific & Isolated**:
   - The worker runs inside the parent's free personal Cloudflare account.
   - Completely isolated from other users.
3. **Generous Free Tier**:
   - Cloudflare's free tier includes 100,000 requests per day (Canvas Tracker uses ~20–50 requests per refresh cycle).
4. **Security Hardening Options**:
   - The worker can be configured to only allow requests originating from the parent's app domain.
   - An optional shared secret header (e.g. `X-Proxy-Secret`) can prevent unauthorized third parties from using the proxy endpoint.

#### Cloudflare Worker Proxy Implementation Reference (~30 lines)
```javascript
export default {
  async fetch(request, env, ctx) {
    // Handle CORS preflight requests
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Proxy-Secret",
          "Access-Control-Max-Age": "86400",
        },
      });
    }

    // Optional proxy secret check
    // if (request.headers.get("X-Proxy-Secret") !== env.PROXY_SECRET) {
    //   return new Response("Unauthorized", { status: 401 });
    // }

    // Parse target Canvas URL (e.g. https://worker.subdomain.workers.dev/https://aacps.instructure.com/api/v1/...)
    const url = new URL(request.url);
    const targetUrl = url.pathname.slice(1) + url.search;

    if (!targetUrl.startsWith("https://")) {
      return new Response("Invalid target URL", { status: 400 });
    }

    // Forward request to Canvas LMS
    const modifiedRequest = new Request(targetUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body,
      redirect: "follow",
    });

    const response = await fetch(modifiedRequest);

    // Return response with permissive CORS headers for the stlite client
    const newHeaders = new Headers(response.headers);
    newHeaders.set("Access-Control-Allow-Origin", "*");
    newHeaders.set("Access-Control-Allow-Methods": "GET, POST, OPTIONS");

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: newHeaders,
    });
  },
};
```

---

## 4. Pluggable Notes Persistence Design

Assignment notes are stored independently from Canvas LMS (which does not provide parent annotation APIs).

### Abstraction (`NotesBackend`)
The abstract interface in [`canvas_notes.py`](canvas_notes.py) specifies:
- `load_all_notes() -> dict[str, dict]`
- `get_note(assignment_id: str | int) -> str`
- `save_note(assignment_id: str | int, note_text: str) -> bool`
- `validate() -> tuple[bool, str]`
- `export_to(target_backend) -> int`

### Keying & Data Schema
- **Key**: Normalized string of Canvas `assignment_id` (e.g. `"1042589"`).
  - Ensures notes for an assignment sync automatically across both Detailed Gradebook and Incomplete Assignments tables.
- **Entry Structure**:
  ```json
  {
    "1042589": {
      "text": "Needs revision on rubric section 3. Contact teacher.",
      "updated_at": "2026-09-23T20:30:00Z"
    }
  }
  ```

### Storage Providers
1. **Local Storage (`LocalNotesBackend`)**:
   - **Desktop**: Reads and writes to `canvas_notes.json` in the project root (git-ignored).
   - **stlite (In-Browser)**: Detects Pyodide environment and transparently switches to `js.localStorage.setItem()` / `getItem()`. Zero code change required.
2. **Google Sheet Sync (`GoogleSheetNotesBackend`)**:
   - For multi-device synchronization (Mac, iPhone, iPad).
   - Uses a Google Apps Script Web App deployed under the parent's Google account.
   - **Write-Through In-Memory Cache**: UI interactions remain instantaneous; network POSTs sync to Google Sheets asynchronously.
   - **No OAuth Token Expiry**: Runs as a Web App URL webhook, eliminating Google Cloud Console OAuth verification warnings and token refresh expirations.
   - **Validation & Migration**: Built-in "🧪 Test Connection" and "⬆️ Export Local Notes" tools in the sidebar.
