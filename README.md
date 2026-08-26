# File Deduplicate Analyzer

A local web application for finding and removing duplicate files, with AI-powered file analysis and smart renaming via AWS Bedrock.

## Features

### 1. Duplicate Detection
- Recursive directory scanning with SHA-256 byte-level hashing
- Size pre-filtering for efficient scanning of large directory trees
- Grouped view showing all duplicate copies with full file paths
- Bulk select/deselect with configurable actions (delete, trash, relocate)
- File extension and size filters

### 2. AI File Analysis (AWS Bedrock)
- Analyze any file type: text, images, video, PDFs, documents, code
- Multiple model support: Claude 3.5 Sonnet, Claude Sonnet 4, Amazon Nova, Titan
- Extracts: description, category, tags, suggested filename, content summary
- Custom analysis prompts for specialized metadata extraction
- Multimodal support: images analyzed visually, videos analyzed frame-by-frame

### 3. Smart Renaming
- Configurable naming convention templates
- Template variables: `{date}`, `{category}`, `{description}`, `{suggested_name}`, `{tags}`, `{ext}`, `{original}`, `{mime}`, `{hash}`, `{counter}`
- Case transformation: lowercase, UPPERCASE, Title Case
- Customizable separators, date formats, and max length
- Preview all renames before applying
- Batch renaming with AI analysis

## Prerequisites

- **Python 3.11+** with pip
- **Node.js 18+** with npm
- **AWS credentials** configured for Bedrock access (`~/.aws/credentials` or environment variables)

### AWS Setup

Ensure your AWS credentials have access to Bedrock models. Set your region:

```
set AWS_REGION=us-east-1
```

Or configure in `~/.aws/config`.

## Quick Start

### Option 1: Start Everything

```powershell
.\start.ps1
```

This creates a Python venv, installs all dependencies, and starts both servers.

### Option 2: Start Separately

Terminal 1 (Backend):
```powershell
.\start-backend.ps1
```

Terminal 2 (Frontend):
```powershell
.\start-frontend.ps1
```

### Option 3: Manual Setup

```powershell
# Backend
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

## Usage

1. Open **http://localhost:3000** in your browser
2. **Scan Directory**: Enter a path and click "Start Scan" to find duplicates
3. **Duplicates**: Review grouped duplicates, select files to remove, choose an action
4. **File Analysis**: Enter a file path, pick an AI model, and analyze
5. **Smart Rename**: Define your naming convention template, add file paths, preview, and apply

## API Endpoints

Backend runs at `http://localhost:8000`. Interactive API docs at `/docs`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check |
| `/api/scanner/scan` | POST | Start directory scan |
| `/api/scanner/status/{id}` | GET | Get scan status |
| `/api/duplicates/{id}` | GET | Get duplicate groups |
| `/api/duplicates/deduplicate` | POST | Remove duplicates |
| `/api/analysis/analyze` | POST | Analyze single file |
| `/api/analysis/analyze-batch` | POST | Analyze multiple files |
| `/api/renaming/preview` | POST | Preview single rename |
| `/api/renaming/preview-bulk` | POST | Preview bulk renames |
| `/api/renaming/apply` | POST | Apply renames |
| `/api/models/` | GET | List available models |

## Naming Convention Template Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `{date}` | File modification date | 2024-01-15 |
| `{category}` | AI-determined category | photo, document, code |
| `{description}` | Short AI description | quarterly_sales_report |
| `{suggested_name}` | AI-suggested filename | budget_analysis_q4 |
| `{tags}` | Top 3 tags joined | finance_report_2024 |
| `{ext}` | File extension | pdf, jpg, py |
| `{original}` | Original filename | IMG_4523 |
| `{mime}` | MIME category | image, video, application |
| `{hash}` | First 8 chars of SHA-256 | a1b2c3d4 |
| `{counter}` | Auto-increment counter | 001 |

### Example Templates

- `{date}_{category}_{suggested_name}.{ext}` → `2024-01-15_photo_sunset_beach.jpg`
- `{category}/{date}_{suggested_name}.{ext}` → organized into category folders
- `{original}_{hash}.{ext}` → preserves original name with hash for uniqueness
- `{counter}_{suggested_name}.{ext}` → numbered sequential files

## Project Structure

```
File-Deduplicate-Analyzer/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI application
│   │   ├── models/
│   │   │   └── schemas.py       # Pydantic request/response models
│   │   ├── routers/
│   │   │   ├── scanner.py       # Scan endpoints
│   │   │   ├── duplicates.py    # Duplicate management
│   │   │   ├── analysis.py      # AI analysis endpoints
│   │   │   ├── renaming.py      # Rename endpoints
│   │   │   └── models.py        # Model listing
│   │   └── services/
│   │       ├── file_scanner.py   # SHA-256 hashing & scanning
│   │       ├── deduplicator.py   # File removal/moving
│   │       ├── bedrock_client.py # AWS Bedrock integration
│   │       └── renaming_service.py # Naming convention engine
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.tsx              # Main app with navigation
│   │   ├── api.ts              # API client
│   │   ├── types.ts            # TypeScript interfaces
│   │   ├── index.css           # Dark theme styles
│   │   └── components/
│   │       ├── ScanPanel.tsx    # Directory scanning UI
│   │       ├── DuplicatesPanel.tsx # Duplicate management UI
│   │       ├── AnalysisPanel.tsx   # AI analysis UI
│   │       └── RenamingPanel.tsx   # Smart renaming UI
│   ├── package.json
│   └── vite.config.ts
├── start.ps1                   # Start both servers
├── start-backend.ps1           # Start backend only
├── start-frontend.ps1          # Start frontend only
└── README.md
```

## Tech Stack

- **Backend**: Python, FastAPI, boto3 (AWS SDK), SHA-256 hashing
- **Frontend**: React 18, TypeScript, Vite
- **AI**: AWS Bedrock (Claude, Nova, Titan models)
- **Styling**: Custom dark theme CSS (no framework dependency)
