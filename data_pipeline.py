import os
from paddleocr import PaddleOCR
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from config import CHUNK_SIZE, TOKEN_OVERLAP

# 初始化PaddleOCR（CPU模式）
ocr = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=False, show_log=False)


def extract_text_from_pdf(file_path: str) -> str:
    """PDF扫描件/电子PDF OCR文字提取"""
    result = ocr.ocr(file_path, cls=True)
    text_lines = []
    for page in result:
        if page:
            for line in page:
                text_lines.append(line[1][0])
    raw_text = "\n".join(text_lines)
    # 基础清洗
    clean_text = raw_text.replace(" ", "").replace("\t", "").strip()
    return clean_text


def split_text(text: str):
    """文本语义分块 - 标题层级切块 + 递归切块 + 80-100token重叠"""
    # Step 1: 标题层级切块（按 Markdown 标题 # / ## / ### 拆分）
    headers_to_split_on = [
        ("#", "h1"),
        ("##", "h2"),
        ("###", "h3"),
    ]

    md_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on,
        strip_headers=True,
    )

    try:
        md_sections = md_splitter.split_text(text)
    except Exception:
        md_sections = None

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

    deduped = []
    for c in all_chunks:
        stripped = c.strip()
        if len(stripped) > 20:
            cleaned = "\n".join(line for line in stripped.split("\n") if line.strip())
            deduped.append(cleaned)

    return deduped


def process_document(file_path: str, doc_name: str):
    """
    完整文档处理入口：解析 -> 清洗 -> 分块 + 附加元数据
    返回: [{"text": str, "metadata": {"source": str, "chunk_id": int}}, ...]
    """
    content = extract_text_from_pdf(file_path)
    if not content:
        return []
    chunks = split_text(content)
    chunk_list = []
    for idx, chunk in enumerate(chunks):
        meta = {
            "source": doc_name,
            "chunk_id": idx
        }
        chunk_list.append({"text": chunk, "metadata": meta})
    return chunk_list