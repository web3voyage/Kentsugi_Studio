# Kentsugi Studio

The React/TypeScript interface for Kentsugi's prompt planning, SVG preview, and
Blender render-job API.

## Prerequisites

- Node.js 22.13 or newer
- The Kentsugi Python backend running on port 8765

## Development

From the repository root, start the backend:

```powershell
python -m kentsugi serve
```

Then start Studio in a second terminal:

```powershell
cd studio
npm install
npm run dev
```

Studio uses `http://127.0.0.1:8765` by default. To use a different backend:

```powershell
$env:NEXT_PUBLIC_KENTSUGI_API_URL = "http://127.0.0.1:9000"
npm run dev
```

## Verification

```powershell
npm run build
npm test
npm run lint
```

The interface submits prompts to `POST /api/jobs`, displays the deterministic
SVG preview, tracks render status, and exposes the completed video when the
Blender worker finishes.
