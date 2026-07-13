# orchestrator/exceptions.py


class OrchestratorNotInitializedError(Exception):
    def __init__(self):
        super().__init__(
            "Call initialize() before handle_utterance()"
        )


class PipelineStepError(Exception):
    def __init__(self, step: int, reason: str):
        self.step = step
        super().__init__(f"Pipeline step {step} failed: {reason}")
