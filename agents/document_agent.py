# ============================================================
# 文档 Agent (DocumentAgent)
# 职责：文档解析、分块、入库管理
# ============================================================
import os
import json
import logging
from typing import List, Dict, Any
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from config import CHUNK_SIZE, TOKEN_OVERLAP, DOCS_DIR

logger = logging.getLogger(__name__)

# chunks 存储目录
CHUNKS_DIR = os.path.join(os.path.dirname(DOCS_DIR) if DOCS_DIR.startswith(".") else os.path.dirname(__file__), "chunks")
os.makedirs(CHUNKS_DIR, exist_ok=True)


class DocumentAgent:
    """
    文档处理 Agent
    职责：
    1. PDF 文档 OCR 解析
    2. 文本智能分块
    3. 文档入库管理（增删改查）
    4. 支持 txt/docx 等格式
    """

    def __init__(self):
        self._ocr = None

    @property
    def ocr(self):
        if self._ocr is None:
            from paddleocr import PaddleOCR  # 懒加载，避免模块级导入触发 NumPy/cv2 冲突
            self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=False, show_log=False)
        return self._ocr

    def extract_text_from_pdf(self, file_path: str) -> str:
        """PDF OCR 文字提取"""
        result = self.ocr.ocr(file_path, cls=True)
        text_lines = []
        for page in result:
            if page:
                for line in page:
                    text_lines.append(line[1][0])
        raw_text = "\n".join(text_lines)
        clean_text = raw_text.replace(" ", "").replace("\t", "").strip()
        return clean_text

    def extract_text_from_txt(self, file_path: str) -> str:
        """TXT 文本提取"""
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def extract_text_from_docx(self, file_path: str) -> str:
        """DOCX 文本提取"""
        try:
            from docx import Document
            doc = Document(file_path)
            return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        except ImportError:
            logger.warning("python-docx not installed, fallback to OCR")
            return self.extract_text_from_pdf(file_path)

    def split_text(self, text: str) -> List[str]:
        """文本语义分块 - 标题层级切块 + 递归切块 + 80-100token重叠"""
        # Step 1: 标题层级切块（按 Markdown 标题 # / ## / ### 拆分）
        headers_to_split_on = [
            ("#", "h1"),
            ("##", "h2"),
            ("###", "h3"),
        ]

        md_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=headers_to_split_on,
            strip_headers=True,   # 剥离标题到 metadata，下方手动拼接避免重复
        )

        try:
            md_sections = md_splitter.split_text(text)
        except Exception:
            md_sections = None

        # 没有检测到标题层级时，整篇文档作为一个 section
        if not md_sections or len(md_sections) <= 1:
            sections = [text]
        else:
            sections = []
            for doc in md_sections:
                headers = doc.metadata
                header_parts = []
                for level in ["h1", "h2", "h3"]:
                    if level in headers and headers[level]:
                        prefix = "#" * int(level[1])
                        header_parts.append(f"{prefix} {headers[level]}")
                header_prefix = "\n".join(header_parts) + "\n" if header_parts else ""
                sections.append(header_prefix + doc.page_content.strip())

        # Step 2: 递归切块，Token 级重叠（80-100 tokens，cl100k_base 编码）
        text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=CHUNK_SIZE,
            chunk_overlap=TOKEN_OVERLAP,
            separators=["\n\n", "\n", "。", "！", "？", "，", " ", ".", ""]
        )

        all_chunks = []
        for section in sections:
            sub_chunks = text_splitter.split_text(section)
            all_chunks.extend(sub_chunks)

        # 去重：清理 token 重叠导致的标题重复行
        deduped = []
        for c in all_chunks:
            stripped = c.strip()
            if len(stripped) > 20:
                cleaned = "\n".join(line for line in stripped.split("\n") if line.strip())
                deduped.append(cleaned)

        return deduped

    def process_document(self, file_path: str, doc_name: str) -> List[Dict]:
        """
        完整文档处理：解析 -> 清洗 -> 分块 + 元数据
        返回: [{"text": str, "metadata": {"source": str, "chunk_id": int}}, ...]
        """
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".pdf":
            content = self.extract_text_from_pdf(file_path)
        elif ext == ".txt":
            content = self.extract_text_from_txt(file_path)
        elif ext == ".docx":
            content = self.extract_text_from_docx(file_path)
        else:
            logger.warning(f"Unsupported file format: {ext}")
            return []

        if not content:
            return []

        chunks = self.split_text(content)
        chunk_list = []
        for idx, chunk in enumerate(chunks):
            chunk_list.append({
                "text": chunk,
                "metadata": {"source": doc_name, "chunk_id": idx}
            })
        return chunk_list

    def save_chunks_to_file(self, doc_name: str, chunks: List[Dict]) -> str:
        """将切片结果保存到 chunks/ 目录下的 JSON 文件"""
        base_name = os.path.splitext(doc_name)[0]
        chunks_file = os.path.join(CHUNKS_DIR, f"{base_name}_chunks.json")
        
        chunk_records = []
        for c in chunks:
            chunk_records.append({
                "chunk_id": c["metadata"]["chunk_id"],
                "source": c["metadata"]["source"],
                "char_count": len(c["text"]),
                "text": c["text"]
            })
        
        data = {
            "doc_name": doc_name,
            "total_chunks": len(chunks),
            "chunks": chunk_records
        }
        
        with open(chunks_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"📄 [chunks] 切片已保存: {chunks_file} | 共 {len(chunks)} 个文本块 | 文档: {doc_name}")
        logger.info(f"💾 切片已保存: {chunks_file} ({len(chunks)} 个块)")
        return chunks_file

    def get_chunks_from_file(self, doc_name: str) -> List[Dict]:
        """从 JSON 文件中读取切片"""
        base_name = os.path.splitext(doc_name)[0]
        chunks_file = os.path.join(CHUNKS_DIR, f"{base_name}_chunks.json")
        if not os.path.exists(chunks_file):
            print(f"⚠️ [chunks] 切片文件不存在: {chunks_file}")
            return []
        with open(chunks_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        chunks = data.get("chunks", [])
        print(f"📖 [chunks] 读取切片: {chunks_file} | {len(chunks)} 个文本块")
        return chunks

    def list_all_chunk_files(self) -> List[Dict]:
        """列出所有切片文件及统计信息"""
        files = []
        if not os.path.exists(CHUNKS_DIR):
            print(f"⚠️ [chunks] chunks 目录不存在: {CHUNKS_DIR}")
            return files
        print(f"📂 [chunks] 扫描 chunks 目录: {CHUNKS_DIR}")
        for fname in os.listdir(CHUNKS_DIR):
            if fname.endswith("_chunks.json"):
                fpath = os.path.join(CHUNKS_DIR, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                chunk_count = data.get("total_chunks", 0)
                doc_name = data.get("doc_name", "")
                files.append({
                    "file": fname,
                    "doc_name": doc_name,
                    "total_chunks": chunk_count,
                    "path": fpath
                })
                print(f"   📄 {fname} -> 文档: {doc_name} | {chunk_count} 个chunk")
        print(f"📊 [chunks] 共发现 {len(files)} 个切片文件")
        return files

    def ingest_document(self, file_path: str, doc_name: str) -> int:
        """
        完整入库流程：解析 + 分块 + 保存到文件 + 向量化 + 写入 Milvus + BM25
        返回: 生成的 chunk 数量
        """
        from core.embedding import embedding_engine
        from core.vector_store import vector_store
        from core.bm25_index import bm25_index

        chunks = self.process_document(file_path, doc_name)
        if not chunks:
            return 0

        # 保存切片到 JSON 文件
        self.save_chunks_to_file(doc_name, chunks)

        texts = [c["text"] for c in chunks]
        sources = [c["metadata"]["source"] for c in chunks]

        # 向量化 + 写 Milvus
        print(f"🧮 [chunks] 开始向量化 {len(texts)} 个文本块 -> Milvus...")
        vecs = embedding_engine.embed_documents(texts)
        vector_store.insert(texts, vecs, sources)
        print(f"✅ [chunks] Milvus 入库完成: {len(texts)} 条向量")

        # 写 BM25 索引
        bm25_index.add_documents(texts, sources)
        print(f"✅ [chunks] BM25 索引已更新: {len(texts)} 篇文档")
        print(f"🎉 [chunks] 入库流程全部完成! doc={doc_name}, chunks={len(chunks)}\n")

        return len(chunks)

    def delete_document(self, doc_name: str):
        """删除指定文档（从 Milvus + BM25 + chunks 文件 中移除）"""
        from core.vector_store import vector_store
        from core.bm25_index import bm25_index

        vector_store.delete_by_source(doc_name)
        bm25_index.remove_by_source(doc_name)

        # 同时删除切片 JSON 文件
        base_name = os.path.splitext(doc_name)[0]
        chunks_file = os.path.join(CHUNKS_DIR, f"{base_name}_chunks.json")
        if os.path.exists(chunks_file):
            os.remove(chunks_file)
            logger.info(f"🗑️ 切片文件已删除: {chunks_file}")

    def list_documents(self) -> List[str]:
        """列出文档目录中的文件"""
        if not os.path.exists(DOCS_DIR):
            return []
        return [f for f in os.listdir(DOCS_DIR) if f.lower().endswith(('.pdf', '.txt', '.docx'))]


# 全局单例
document_agent = DocumentAgent()
