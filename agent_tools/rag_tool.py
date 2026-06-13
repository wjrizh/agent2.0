import os
import sys
import time
from .core import BaseTool

ROCM_SITE = os.path.expanduser("/home/z/shenduxuexi/venv/lib/python3.12/site-packages")

# 禁止索引的目录（深度 <= 此值不允许 build）
MAX_BUILD_DEPTH = 3


def _load_embedding_model():
    if ROCM_SITE not in sys.path:
        sys.path.insert(0, ROCM_SITE)

    import torch
    from transformers import AutoTokenizer, AutoModel

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_name = "BAAI/bge-small-zh-v1.5"
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name).to(device)
    model.eval()

    def encode(texts):
        if isinstance(texts, str):
            texts = [texts]
        with torch.no_grad():
            inputs = tokenizer(texts, padding=True, truncation=True,
                               max_length=512, return_tensors="pt").to(device)
            outputs = model(**inputs)
            attention_mask = inputs["attention_mask"]
            embeddings = (outputs.last_hidden_state * attention_mask.unsqueeze(-1)).sum(1)
            embeddings = embeddings / attention_mask.sum(1, keepdim=True)
            embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
        return embeddings.cpu().numpy()

    return encode


class RagTool(BaseTool):
    name = "rag_tool"
    description = (
        "Codebase semantic search (RAG). "
        "Index is stored in .ligong_index under the project directory. "
        "Actions: 'build' (full rebuild), 'search' (semantic query), "
        "'delete' (remove index), 'list' (show existing index info)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["build", "search", "delete", "list"],
                "description": (
                    "'build' = full rebuild index. "
                    "'search' = semantic query. "
                    "'delete' = remove the entire index. "
                    "'list' = show existing index info (path and chunk count)."
                )
            },
            "query": {
                "type": "string",
                "description": "Required for 'search'. Natural language query."
            },
            "path": {
                "type": "string",
                "description": "Optional. Defaults to current working directory."
            }
        },
        "required": ["action"]
    }
    timeout = 300

    def run(self, action: str, query: str = "", path: str = None, **kwargs) -> str:
        try:
            encode = _load_embedding_model()
        except Exception as e:
            return f"Error loading embedding model: {e}"

        try:
            import chromadb
            from langchain_text_splitters import RecursiveCharacterTextSplitter, Language
        except ImportError:
            return "Error: Missing dependencies. Run `pip install chromadb langchain-text-splitters`"

        base_path = os.path.abspath(path) if path else os.getcwd()
        db_path = os.path.join(base_path, ".ligong_index")

        def get_embedding(text):
            return encode(text)[0].tolist()

        # ===== list =====
        if action == "list":
            if not os.path.isdir(db_path):
                return f"No RAG index found at {base_path}."
            try:
                c = chromadb.PersistentClient(path=db_path)
                col = c.get_collection("codebase_index")
                return f"Index at {base_path}: {col.count()} chunks."
            except Exception:
                return f"No RAG index found at {base_path}. Run rag_tool with action='build' first."

        # ===== delete =====
        if action == "delete":
            try:
                client = chromadb.PersistentClient(path=db_path)
                client.delete_collection("codebase_index")
                return f"Index deleted from {base_path}"
            except Exception:
                return f"No index found at {base_path} to delete."

        # ===== search =====
        if action == "search":
            if not query:
                return "Error: 'query' is required for search."
            try:
                client = chromadb.PersistentClient(path=db_path)
                collection = client.get_collection("codebase_index")
            except Exception:
                return f"Error: No index found at {base_path}. Run rag_tool with action='build' first."
            if collection.count() == 0:
                return "Error: The codebase index is empty."

            query_embedding = get_embedding(query)
            results = collection.query(query_embeddings=[query_embedding], n_results=5)

            if not results['documents'] or not results['documents'][0]:
                return f"No relevant code found for query: '{query}'"

            out = [f"=== SEMANTIC SEARCH RESULTS FOR: '{query}' ==="]
            docs = results['documents'][0]
            metas = results['metadatas'][0]
            for i in range(len(docs)):
                src = metas[i].get('source', 'Unknown')
                out.append(f"--- File: {src} ---\n{docs[i]}\n")
            return "\n".join(out)

        # ===== build =====
        if action == "build":
            # 安全检查：拒绝在根目录或主目录构建索引
            depth = len(base_path.rstrip('/').split('/'))
            if depth <= MAX_BUILD_DEPTH:
                return (
                    f"Safety: '{base_path}' is too high-level (depth {depth} <= {MAX_BUILD_DEPTH}). "
                    f"This would scan too many files. Please specify a project subdirectory, "
                    f"e.g., path='{os.path.join(base_path, 'your-project')}'."
                )

            start_time = time.time()
            supported_exts = {
                ".py": Language.PYTHON, ".js": Language.JS, ".ts": Language.TS,
                ".cpp": Language.CPP, ".go": Language.GO, ".java": Language.JAVA,
                ".html": Language.HTML, ".md": Language.MARKDOWN
            }

            client = chromadb.PersistentClient(path=db_path)
            try:
                client.delete_collection("codebase_index")
            except Exception:
                pass
            collection = client.get_or_create_collection("codebase_index")

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
                        language=supported_exts[ext], chunk_size=800, chunk_overlap=100
                    )
                    chunks = splitter.create_documents([content])
                    for i, chunk in enumerate(chunks):
                        docs_to_insert.append(chunk.page_content)
                        metadatas.append({"source": rel_path, "chunk_id": i})
                        ids.append(f"{rel_path}_{i}")

            if not docs_to_insert:
                return f"No supported code files found in {base_path} to index."

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
            return f"Index built successfully. Indexed {len(docs_to_insert)} code chunks from {base_path} in {cost_time:.1f}s."

        return "Error: Invalid action. Choose 'build', 'search', 'delete', or 'list'."