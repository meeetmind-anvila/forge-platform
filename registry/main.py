"""
Forge Artifact Registry — FastAPI application.
Endpoints: /artifacts/{name}/{version}, /artifacts/{name}
"""
import asyncio
import hashlib
import io
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
