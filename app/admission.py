"""A channel's pre-submission policy denial is not invalid caller input."""
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


class AdmissionRejected(HTTPException):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(status_code=503, detail=message, headers={
            "X-IJMAPI-Submit-Outcome": "not-accepted", "X-IJMAPI-Error-Code": code,
        })


async def admission_rejected_response(_: Request, exc: AdmissionRejected) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, headers=exc.headers, content={
        "detail": exc.detail,
        "error": {"code": exc.code, "message": exc.detail, "type": "upstream_unavailable",
                  "retryable": True, "phase": "admission", "failure_scope": "channel",
                  "generation_dispatched": False, "outcome": "not_accepted"},
        "submission_outcome": "not-accepted",
    })


def register_admission_errors(app: FastAPI) -> None:
    app.add_exception_handler(AdmissionRejected, admission_rejected_response)
