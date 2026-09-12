"""Deterministic integration fixture; never use its scores as benchmark results.

Load with PYTHONPATH=harbor/tests and -a smoke_agent:SmokeAgent. This exercises
Harbor's custom-agent boundary without contacting a model provider.
"""

from harbor.agents.nop import NopAgent


class SmokeAgent(NopAgent):
    @staticmethod
    def name() -> str:
        return "jobbench-smoke-fixture"

    async def run(self, instruction, environment, context) -> None:
        result = await environment.exec(
            command="""python - <<'PY'
import importlib
import json
import os
from pathlib import Path
import shutil
import urllib.request

root = Path('/workspace')
assert (root / 'task_folder/TASK_INSTRUCTIONS.txt').is_file()
assert not Path('/tests').exists(), 'verifier assets exposed to agent'
assert 'JUDGE_API_KEY' not in os.environ, 'judge credential exposed to agent'
for filename in ('RUBRICS.json', 'task_card.md'):
    assert not list(root.rglob(filename)), filename + ' exposed to agent'
assert not list(root.rglob('files_required_to_search'))
for module in ('pandas', 'openpyxl', 'xlrd', 'scipy', 'statsmodels',
               'matplotlib', 'docx', 'pptx', 'pdfplumber', 'fitz',
               'PIL', 'trimesh', 'geopandas', 'libpysal', 'esda'):
    importlib.import_module(module)
for tool in ('curl', 'jq', 'pdftoppm', 'pdftotext', 'tesseract', 'libreoffice'):
    assert shutil.which(tool), tool + ' unavailable'
with urllib.request.urlopen('https://huggingface.co', timeout=30) as response:
    assert response.status == 200

import pandas as pd
from docx import Document
from PIL import Image

output = root / 'output'
output.mkdir(exist_ok=True)
(output / 'smoke.txt').write_text('Deterministic integration fixture. Not a task solution.\\n')
pd.DataFrame({'item': ['smoke'], 'value': [7]}).to_excel(output / 'smoke.xlsx', index=False)
document = Document()
document.add_paragraph('Deterministic document fixture.')
document.save(output / 'smoke.docx')
Image.new('RGB', (16, 16), 'blue').save(output / 'smoke.png')
print(json.dumps({'input_files': len(list((root / 'task_folder').iterdir())),
                  'output_files': len(list(output.iterdir())), 'network': 'ok'}))
PY""",
            timeout_sec=180,
        )
        if result.return_code:
            raise RuntimeError(f"Smoke fixture failed: {result.stdout}\n{result.stderr}")
        context.cost_usd = 0.0
        context.n_input_tokens = 0
        context.n_output_tokens = 0
        context.metadata = {"fixture": True, "checks": result.stdout}
