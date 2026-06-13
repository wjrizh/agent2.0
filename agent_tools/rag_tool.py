import os
import sys
import time
import subprocess
from .core import BaseTool

ROCM_PYTHON = os.path.expanduser("/home/z/shenduxuexi/venv/bin/python3")

def _ensure_torch():
    """智能环境检测：优先复用已有 ROCm PyTorch，否则自动装 CPU 版"""
    try:
        import torch
        return torch
    except ImportError:
        pass
    if os.path.exists(ROCM_PYTHON):
        rocm_site = os.path.expanduser("/home/z/shenduxuexi/venv/lib/python3.12/site-packages")
        if rocm_site not in sys.path:
            sys.path.insert(0, rocm_site)
        try:
            import torch
            return torch
        except ImportError:
            pass
    subprocess.check_call([sys.executable, "-m", "pip", "install", "torch",
        "--index-url", "https://download.pytorch.org/whl/cpu", "-q"])
    import torch
    return torch


class RagTool(BaseTool):
    name = "rag_tool"
    description = "Codebase semantic search (RAG). Use 'build' to index the project directory first. Use 'search' to find semantically relevant code snippets based on natural language queries."
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["build", "search"],
                "description": "Choose 'build' to index the codebase, or 'search' to query the index."
            },
            "query": {
                "type": "string",
                "description": "Required for 'search'. The natural language query (e.g., 'where is the network timeout logic?')."
            },
            "path": {
                "type": "string",
                "description": "Optional. The root directory to build index for. Defaults to current working directory."
            }
        },
        "required": ["action"]
    }
    timeout = 300

    def run(self, action: str, query: str = "", path: str = None, **kwargs) -> str:
        try:
            import chromadb
            from langchain_text_splitters import RecursiveCharacterTextSplitter, Language
            from sentence_transformers import SentenceTransformer
        except ImportError:
            return "Error: Missing dependencies. Please run `pip install chromadb sentence-transformers langchain-text-splitters`"

        base_path = os.path.abspath(path) if path else os.getcwd()
        db_path = os.path.join(base_path, ".ligong_index")
        embedding_model_name = "BAAI/bge-small-zh-v1.5"

        def get_embedding(text):
            if not hasattr(self, "_model"):
                self._model = SentenceTransformer(embedding_model_name)
            return self._model.encode(text).tolist()

        client = chromadb.PersistentClient(path=db_path)
        collection = client.get_or_create_collection(name="codebase_index")

        if action == "build":
            start_time = time.time()
            supported_exts = {
                ".py": Language.PYTHON, ".js": Language.JS, ".ts": Language.TS,
                ".cpp": Language.CPP, ".go": Language.GO, ".java": Language.JAVA,
                ".html": Language.HTML, ".md": Language.MARKDOWN
            }

            docs_to_insert = []
            metadatas = []
            ids = []

            for root, dirs, files in os.walk(base_path):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('venv', '__pycache__', 'node_modules')]

                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext not in supported_exts:
                        continue

                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, base_path)

                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                    except Exception:
                        continue

                    splitter = RecursiveCharacterTextSplitter.from_language(
                        language=supported_exts[ext],
                        chunk_size=800,
                        chunk_overlap=100
                    )
                    chunks = splitter.create_documents([content])

                    for i, chunk in enumerate(chunks):
                        docs_to_insert.append(chunk.page_content)
                        metadatas.append({"source": rel_path, "chunk_id": i})
                        ids.append(f"{rel_path}_{i}")

            if not docs_to_insert:
                return f"No supported code files found in {base_path} to index."

            if collection.count() > 0:
                client.delete_collection("codebase_index")
                collection = client.create_collection("codebase_index")

            batch_size = 500
            for i in range(0, len(docs_to_insert), batch_size):
                end = min(i + batch_size, len(docs_to_insert))
                embeddings = [get_embedding(doc) for doc in docs_to_insert[i:end]]
                collection.add(
                    documents=docs_to_insert[i:end],
                    embeddings=embeddings,
                    metadatas=metadatas[i:end],
                    ids=ids[i:end]
                )

            cost_time = time.time() - start_time
            return f"Index built successfully. Indexed {len(docs_to_insert)} code chunks from {base_path} in {cost_time:.1f} seconds."

        elif action == "search":
            if not query:
                return "Error: 'query' is required for search."

            if collection.count() == 0:
                return "Error: The codebase index is empty. Please run rag_tool with action='build' first."

            query_embedding = get_embedding(query)
            results = collection.query(query_embeddings=[query_embedding], n_results=5)

            if not results['documents'] or not results['documents'][0]:
                return f"No relevant code found for query: '{query}'"

            out = [f"=== SEMANTIC SEARCH RESULTS FOR: '{query}' ==="]
            docs = results['documents'][0]
            metas = results['metadatas'][0]

            for i in range(len(docs)):
                src = metas[i].get('source', 'Unknown')
                content = docs[i]
                out.append(f"--- File: {src} ---\n{content}\n")

            return "\n".join(out)

        else:
            return "Error: Invalid action. Choose 'build' or 'search'."