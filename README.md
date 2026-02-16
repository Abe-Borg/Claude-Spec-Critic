# MEP Spec Review v1.0.0

A desktop tool that reviews mechanical and plumbing construction specifications for California K-12 DSA projects using Claude Opus 4.6. Load `.docx` spec files, run the review, and see color-coded findings rendered directly in the app.

## What It Does

1. Extracts text from `.docx` specification files (paragraphs + tables)
2. Detects LEED references and unresolved placeholders locally (no API call needed)
3. Performs pre-flight token analysis with an animated visual gauge
4. Sends combined spec content to Claude Opus 4.6 via streaming API
5. Streams Claude's analysis in real-time with a sassy senior-engineer personality
6. Parses structured JSON findings from the response
7. Renders a full report in-app: summary grid, alerts, severity-colored finding cards, reviewer's notes

## Running the Application

```bash
python main.py
```

### First-Time Setup

```bash
# Clone the repo
git clone <your-repo-url>
cd spec-review

# Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux

# Install dependencies
pip install -e .

# Run
python main.py
```

## API Key

The tool looks for your Anthropic API key in this order:

1. `spec_critic_api_key.txt` file in the same directory as the executable (or project root during development)
2. `ANTHROPIC_API_KEY` environment variable
3. Manual entry in the API Key field within the app

For day-to-day use, drop a `spec_critic_api_key.txt` file next to the executable (or in the project root) containing just your key. The app will auto-load it on launch.

## How to Use

1. Launch the app with `python main.py`
2. Enter your API key (or let it auto-load from file)
3. Click **Folder** to select a directory of `.docx` specs, or **Files** to pick individual files
4. The token gauge fills to show capacity usage — stay under the 150k limit
5. Expand the **FILES** panel to check/uncheck individual specs if needed
6. Click **Run Review**
7. Watch Claude's analysis stream in real-time
8. When complete, the report panel appears with all findings

### Report Panel

After the review completes, the report panel renders:

- **Summary grid**: Five color-coded cards showing Critical, High, Medium, Gripes, and Total counts
- **Token/time metadata**: Input/output token counts and processing duration
- **Alerts**: LEED references and unresolved placeholders detected locally (grouped by file)
- **Findings**: Cards grouped by severity (CRITICAL → HIGH → MEDIUM → GRIPES), each showing:
  - Severity badge and filename
  - Section reference (CSI format)
  - Issue description
  - Existing text in red monospace
  - Replacement text in green monospace
  - Code reference in blue
- **Reviewer's Notes**: Claude's personality-driven analysis summary

### Export Options

- **Export JSON**: Opens a save dialog to write findings, alerts, and metadata to a `.json` file
- **Copy Summary**: Copies the reviewer's analysis summary text to your clipboard

## Project Structure

```
spec-review/
├── src/
│   ├── __init__.py      # Package version
│   ├── gui.py           # Main application window
│   ├── widgets.py       # Custom UI widgets (TokenGauge, FileListPanel,
│   │                    #   EnhancedLog, StreamingPanel, AnimatedButton,
│   │                    #   ReportPanel)
│   ├── pipeline.py      # Core orchestration (single source of truth)
│   ├── extractor.py     # DOCX text extraction
│   ├── preprocessor.py  # LEED/placeholder detection (no mutation)
│   ├── tokenizer.py     # Token counting with tiktoken
│   ├── prompts.py       # System prompt for Claude
│   └── reviewer.py      # Anthropic API client with streaming + retry
├── main.py              # Entry point (also PyInstaller target)
├── pyproject.toml       # Project metadata & dependencies
└── README.md
```

## Architecture

### Design Decisions

- **Single pipeline**: All workflow logic lives in `pipeline.py`. The GUI is a thin shell that calls `run_review()` and renders the result. This eliminates drift and makes testing straightforward.
- **Single model**: Hardcoded to Claude Opus 4.6 (`claude-opus-4-6`). No model selection flags, no alternatives.
- **No document mutation**: This repo only analyzes specs. Document cleanup belongs in the separate SpecCleanse tool.
- **No file output**: All results render in-app. The only file output is the optional Export JSON button. No output directories, no intermediate files.
- **Advisory only**: This tool assists human reviewers. It is not an AHJ substitute.
- **Streaming first**: Claude's response streams in real-time to the StreamingPanel. When streaming completes, findings are parsed and rendered in the ReportPanel. All other panels auto-collapse during streaming so Claude's analysis takes center stage.

### Module Responsibilities

| Module | Purpose |
|---|---|
| `gui.py` | App window, input handling, threading, review orchestration |
| `widgets.py` | All custom CustomTkinter widgets with animations |
| `pipeline.py` | Single source of truth for the review workflow |
| `extractor.py` | DOCX text extraction (paragraphs + tables) |
| `preprocessor.py` | Local detection of LEED refs and placeholders |
| `tokenizer.py` | Token counting via tiktoken, limit enforcement |
| `prompts.py` | System prompt with personality and severity definitions |
| `reviewer.py` | Anthropic API client, streaming, JSON parsing, retry logic |

### Data Flow

```
.docx files
    → extractor.py (text extraction)
    → preprocessor.py (LEED/placeholder detection, local only)
    → tokenizer.py (token counting, limit check)
    → reviewer.py (streaming API call to Claude Opus 4.6)
    → pipeline.py (orchestration, returns PipelineResult)
    → gui.py (renders ReportPanel with findings)
```

## What Claude Reviews

The system prompt instructs Claude to check:

- Code compliance: CBC, CMC, CPC, California Energy Code, CALGreen
- DSA-specific requirements: seismic restraint, certification, submittals
- ASHRAE standards: 62.1, 90.1, 55, etc.
- SMACNA standards: duct construction, seismic restraint
- ASPE standards: plumbing engineering practice
- NFPA standards: fire pumps, special hazards
- MSS, ASTM standards: pipe hangers, materials, testing
- Product specifications: manufacturer names, model numbers, ratings
- Internal consistency: within each spec and across multiple specs
- Coordination: mechanical vs. plumbing, and cross-discipline if non-MEP specs are included
- Constructability: issues that could cause delays or cost overruns

Claude classifies findings into four severity levels:

- **CRITICAL**: DSA rejection risks, code violations, safety hazards
- **HIGH**: Significant technical errors, outdated CSI formatting, coordination conflicts
- **MEDIUM**: Wrong code years, discontinued products, minor inconsistencies
- **GRIPES**: Typos, formatting issues, overly restrictive requirements

LEED references and unresolved placeholders (`[INSERT]`, `[VERIFY]`, `[TBD]`, etc.) are detected locally by `preprocessor.py` and displayed as alerts — they are not sent to Claude.

## Building the Executable

To create a standalone `.exe` that doesn't require Python:

```bash
pip install pyinstaller
pyinstaller spec-review.spec --clean
```

The executable will be created at `dist/MEP-Spec-Review.exe`.

**Using the executable:**

1. Place `spec_critic_api_key.txt` in the same folder as the `.exe`
2. Run `MEP-Spec-Review.exe`
3. Select your specs folder or individual files
4. Click "Run Review"

## Troubleshooting

### Token Limit Exceeded

If the token gauge shows "Capacity Exceeded" and turns red, your combined specs exceed the 150k token input limit. Use the FILES panel to uncheck some specs and bring the count under the limit. The run button is disabled while over capacity.

### API Key Not Loading

Make sure `spec_critic_api_key.txt` is in the project root (during development) or next to the `.exe` (when using the compiled version). The file should contain only the API key with no extra whitespace or newlines.

### Streaming Stalls or Errors

Claude Opus 4.6 with large context windows requires streaming. If you see connection errors, the reviewer will automatically retry with exponential backoff (up to 3 attempts). Rate limit errors wait 10s/20s/40s between retries; connection errors wait 5s/10s/20s.

## Dependencies

```
anthropic          # Claude API client
python-docx        # DOCX text extraction
tiktoken           # Token counting (cl100k_base encoding)
customtkinter      # Modern themed Tkinter widgets
```

## Changelog

### v1.0.0

- Replaced Word document output with in-app ReportPanel
- Extracted all custom widgets into `widgets.py`
- Moved `gui.py` into `src/` package
- Removed all file output (no report.docx, findings.json, raw_response.txt, etc.)
- Removed CLI mode, debug mode, output folder picker
- Updated system prompt to HTML version (richer severity definitions, cross-discipline coordination, no brevity constraint, CRITICAL CHECKS section)
- Updated model reference to Claude Opus 4.6
- Simplified `pipeline.py` to return in-memory `PipelineResult` only