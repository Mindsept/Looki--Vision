import asyncio

from app.core.config import Settings
from app.core.database import Database
from app.providers.centres import ContextProvider
from app.services.analysis import AnalysisWorker
from app.services.frames import FrameService
from app.services.questions import QuestionService
from app.services.sessions import SessionService


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings)
        # One API process/replica: admission and event commits share this lock.
        self.write_lock = asyncio.Lock()
        self.centres = ContextProvider(settings)
        self.vision = None
        self.worker_task: asyncio.Task | None = None
        self.sessions = SessionService(self)
        self.frames = FrameService(self)
        self.questions = QuestionService(self)
        self.worker = AnalysisWorker(self)
