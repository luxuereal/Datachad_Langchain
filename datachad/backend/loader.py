import os
import re
import shutil
from pathlib import Path

from langchain.document_loaders.base import BaseLoader
from langchain.schema import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import (
    CSVLoader,
    EverNoteLoader,
    GitLoader,
    NotebookLoader,
    OnlinePDFLoader,
    PyPDFium2Loader,
    PythonLoader,
    TextLoader,
    UnstructuredEPubLoader,
    UnstructuredFileLoader,
    UnstructuredHTMLLoader,
    UnstructuredMarkdownLoader,
    UnstructuredODTLoader,
    UnstructuredPowerPointLoader,
    UnstructuredWordDocumentLoader,
    WebBaseLoader,
)
from tqdm import tqdm

from datachad.backend.constants import DATA_PATH
from datachad.backend.logging import logger
from datachad.backend.models import STORES, get_tokenizer


class SmartFAQSplitter:
    def split_documents(self, documents: list[Document]) -> list[Document]:
        """
        Splits the given text into a list of strings based on the regex patterns of numbered lists.
        Each new list item is separated by two blank lines like this:

            1. First item
            Some description here.

            1. some numbered list
            2. beloing to the first item


            2. Second item
            Another description.

            a) another list
            b) but with characters


            3. Third item
            And another one.
            - a list with dashes
            - more items
        """
        splitted_documents = []
        for document in documents:
            split_text = re.split(r"(?=\n\n\d+\.)", document.page_content.strip())
            filtered_text = [re.sub(r"^\n+|\n+$", "", section) for section in split_text]
            splitted_documents.extend(
                [
                    Document(
                        page_content=text,
                        metadata={"faq_no": int(re.findall(r"\d", text)[0])},
                    )
                    for text in filtered_text
                ]
            )
        return splitted_documents


class AutoGitLoader:
    def __init__(self, data_source: str) -> None:
        self.data_source = data_source

    def load(self) -> list[Document]:
        # We need to try both common main branches
        # Thank you github for the "master" to "main" switch
        # we need to make sure the data path exists
        if not os.path.exists(DATA_PATH):
            os.makedirs(DATA_PATH)
        repo_name = self.data_source.split("/")[-1].split(".")[0]
        repo_path = str((DATA_PATH / repo_name).absolute())
        clone_url = self.data_source
        if os.path.exists(repo_path):
            clone_url = None
        branches = ["main", "master"]
        for branch in branches:
            try:
                docs = GitLoader(repo_path, clone_url, branch).load()
                break
            except Exception as e:
                logger.error(f"Error loading git: {e}")
        if os.path.exists(repo_path):
            # cleanup repo afterwards
            shutil.rmtree(repo_path)
        try:
            return docs
        except:
            raise RuntimeError("Error loading git. Make sure to use HTTPS GitHub repo links.")


FILE_LOADER_MAPPING = {
    ".csv": (CSVLoader, {"encoding": "utf-8"}),
    ".doc": (UnstructuredWordDocumentLoader, {}),
    ".docx": (UnstructuredWordDocumentLoader, {}),
    ".enex": (EverNoteLoader, {}),
    ".epub": (UnstructuredEPubLoader, {}),
    ".html": (UnstructuredHTMLLoader, {}),
    ".md": (UnstructuredMarkdownLoader, {}),
    ".odt": (UnstructuredODTLoader, {}),
    ".pdf": (PyPDFium2Loader, {}),
    ".ppt": (UnstructuredPowerPointLoader, {}),
    ".pptx": (UnstructuredPowerPointLoader, {}),
    ".txt": (TextLoader, {"encoding": "utf8"}),
    ".ipynb": (NotebookLoader, {}),
    ".py": (PythonLoader, {}),
    # Add more mappings for other file extensions and loaders as needed
}

WEB_LOADER_MAPPING = {
    ".git": (AutoGitLoader, {}),
    ".pdf": (OnlinePDFLoader, {}),
}


def load_document(
    file_path: str,
    mapping: dict = FILE_LOADER_MAPPING,
    default_loader: BaseLoader = UnstructuredFileLoader,
) -> Document:
    # Choose loader from mapping, load default if no match found
    ext = "." + file_path.rsplit(".", 1)[-1]
    if ext in mapping:
        loader_class, loader_args = mapping[ext]
        loader = loader_class(file_path, **loader_args)
    else:
        loader = default_loader(file_path)
    return loader.load()


def load_directory(path: str, silent_errors=True) -> list[Document]:
    # We don't load hidden files starting with "."
    all_files = list(Path(path).rglob("**/[!.]*"))
    results = []
    with tqdm(total=len(all_files), desc="Loading documents", ncols=80) as pbar:
        for file in all_files:
            try:
                results.extend(load_document(str(file)))
            except Exception as e:
                if silent_errors:
                    logger.error(f"failed to load {file}")
                else:
                    raise e
            pbar.update()
    return results


def load_data_source(data_source: str) -> list[Document]:
    # Ugly thing that decides how to load data
    # It aint much, but it's honest work
    is_web = data_source.startswith("http")
    is_dir = os.path.isdir(data_source)
    is_file = os.path.isfile(data_source)
    try:
        if is_dir:
            docs = load_directory(data_source)
        elif is_file:
            docs = load_document(data_source)
        elif is_web:
            docs = load_document(data_source, WEB_LOADER_MAPPING, WebBaseLoader)
        else:
            raise TypeError
        return docs
    except Exception as e:
        error_msg = f"Failed to load your data source '{data_source}'."
        logger.error(error_msg)
        e.args += (error_msg,)
        raise e


def split_docs(docs: list[Document], store_type: str, options: dict) -> list[Document]:
    if store_type == STORES.SMART_FAQ:
        text_splitter = SmartFAQSplitter()
    else:
        tokenizer = get_tokenizer(options)

        def length_function(text: str) -> int:
            # count chunks like the embeddings model tokenizer does
            return len(tokenizer.encode(text))

        chunk_overlap = int(options["chunk_size"] * options["chunk_overlap_pct"] / 100)
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=options["chunk_size"],
            chunk_overlap=chunk_overlap,
            length_function=length_function,
            separators=["\n\n", "#", "\.", "!", "\?", "\n", ",", " ", ""],
        )

    splitted_docs = text_splitter.split_documents(docs)
    logger.info(f"Loaded: {len(splitted_docs)} document chucks")
    return splitted_docs
