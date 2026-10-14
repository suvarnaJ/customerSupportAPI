
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated
from uuid import uuid4
import os

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from pymongo import AsyncMongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

load_dotenv()

MONGODB_URL = os.getenv(
    "MONGODB_URL", "mongodb://localhost:27017"
)
MONGODB_DATABASE = os.getenv(
    "MONGODB_DATABASE", "customer_support"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Category(str, Enum):
    TECHNICAL = "technical"
    BILLING = "billing"
    PAYMENT = "payment"
    ACCOUNT = "account"
    GENERAL = "general"


class Customer(BaseModel):
    customer_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=150)
    email: EmailStr


class CommentInput(BaseModel):
    author: str = Field(min_length=1, max_length=150)
    message: str = Field(min_length=1, max_length=5000)


class Comment(CommentInput):
    comment_id: str
    created_at: datetime


class TicketCreate(BaseModel):
    customer: Customer
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=1, max_length=10000)
    category: Category = Category.GENERAL
    priority: Priority = Priority.MEDIUM
    status: TicketStatus = TicketStatus.OPEN
    tags: list[str] = Field(default_factory=list, max_length=30)


class TicketUpdate(BaseModel):
    customer: Customer | None = None
    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(
        default=None, min_length=1, max_length=10000
    )
    category: Category | None = None
    priority: Priority | None = None
    status: TicketStatus | None = None
    tags: list[str] | None = Field(default=None, max_length=30)


class StatusUpdate(BaseModel):
    status: TicketStatus


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncMongoClient(MONGODB_URL)
    app.state.mongo_client = client
    app.state.tickets = client[MONGODB_DATABASE]["tickets"]

    # Verify database connectivity during startup.
    await client.admin.command("ping")

    tickets = app.state.tickets
    await tickets.create_index([("created_at", DESCENDING)])
    await tickets.create_index([("status", ASCENDING)])
    await tickets.create_index([("priority", ASCENDING)])
    await tickets.create_index([("customer.customer_id", ASCENDING)])
    await tickets.create_index([("customer.email", ASCENDING)])
    await tickets.create_index(
        [
            ("title", "text"),
            ("description", "text"),
            ("customer.name", "text"),
            ("customer.email", "text"),
        ],
        name="ticket_text_search",
    )

    try:
        yield
    finally:
        await client.close()


app = FastAPI(
    title="Customer Support Ticket API",
    description="REST API for managing customer support tickets.",
    version="1.0.0",
    lifespan=lifespan,
)


def get_collection(request: Request):
    return request.app.state.tickets


def serialize_ticket(ticket: dict) -> dict:
    """Convert MongoDB ObjectId into a JSON-friendly string."""
    ticket["id"] = str(ticket.pop("_id"))
    return ticket


def parse_ticket_id(ticket_id: str) -> ObjectId:
    if not ObjectId.is_valid(ticket_id):
        raise HTTPException(
            status_code=400, detail="Invalid ticket ID"
        )
    return ObjectId(ticket_id)


async def find_ticket(collection, ticket_id: str) -> dict:
    oid = parse_ticket_id(ticket_id)
    ticket = await collection.find_one({"_id": oid})

    if not ticket:
        raise HTTPException(
            status_code=404, detail="Ticket not found"
        )
    return ticket


@app.get("/health")
async def health(request: Request):
    await request.app.state.mongo_client.admin.command("ping")
    return {"status": "ok", "database": "connected"}


@app.post(
    "/tickets",
    status_code=status.HTTP_201_CREATED,
)
async def create_ticket(
    payload: TicketCreate,
    request: Request,
):
    collection = get_collection(request)
    now = utc_now()

    ticket = payload.model_dump(mode="python")
    ticket["comments"] = []
    ticket["created_at"] = now
    ticket["updated_at"] = now

    result = await collection.insert_one(ticket)
    ticket["_id"] = result.inserted_id

    return serialize_ticket(ticket)


@app.get("/tickets")
async def list_tickets(
    request: Request,
    priority: Priority | None = None,
    status_filter: Annotated[
        TicketStatus | None, Query(alias="status")
    ] = None,
    category: Category | None = None,
    customer_id: str | None = None,
    tag: str | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
):
    collection = get_collection(request)
    filters = {}

    if priority:
        filters["priority"] = priority.value
    if status_filter:
        filters["status"] = status_filter.value
    if category:
        filters["category"] = category.value
    if customer_id:
        filters["customer.customer_id"] = customer_id
    if tag:
        filters["tags"] = tag

    if q:
        # Escape user input to avoid interpreting arbitrary regex syntax.
        import re
        filters["$or"] = [
            {"title": {"$regex": re.escape(q), "$options": "i"}},
            {"description": {"$regex": re.escape(q), "$options": "i"}},
            {"customer.name": {"$regex": re.escape(q), "$options": "i"}},
            {"customer.email": {"$regex": re.escape(q), "$options": "i"}},
        ]

    total = await collection.count_documents(filters)
    cursor = (
        collection.find(filters)
        .sort("created_at", DESCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = [
        serialize_ticket(ticket)
        async for ticket in cursor
    ]

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": (total + page_size - 1) // page_size,
    }


@app.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, request: Request):
    ticket = await find_ticket(
        get_collection(request), ticket_id
    )
    return serialize_ticket(ticket)


@app.patch("/tickets/{ticket_id}")
async def update_ticket(
    ticket_id: str,
    payload: TicketUpdate,
    request: Request,
):
    collection = get_collection(request)
    oid = parse_ticket_id(ticket_id)

    # Exclude omitted fields, but retain explicit null values so
    # the following validation can reject them for required fields.
    changes = payload.model_dump(
        exclude_unset=True, mode="python"
    )

    if not changes:
        raise HTTPException(
            status_code=400, detail="No fields supplied"
        )

    if any(value is None for value in changes.values()):
        raise HTTPException(
            status_code=422,
            detail="Ticket fields cannot be null",
        )

    changes["updated_at"] = utc_now()

    result = await collection.update_one(
        {"_id": oid}, {"$set": changes}
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=404, detail="Ticket not found"
        )

    ticket = await collection.find_one({"_id": oid})
    return serialize_ticket(ticket)


@app.delete(
    "/tickets/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_ticket(ticket_id: str, request: Request):
    result = await get_collection(request).delete_one(
        {"_id": parse_ticket_id(ticket_id)}
    )

    if result.deleted_count == 0:
        raise HTTPException(
            status_code=404, detail="Ticket not found"
        )


@app.post(
    "/tickets/{ticket_id}/comments",
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    ticket_id: str,
    payload: CommentInput,
    request: Request,
):
    collection = get_collection(request)
    oid = parse_ticket_id(ticket_id)

    comment = payload.model_dump()
    comment["comment_id"] = str(uuid4())
    comment["created_at"] = utc_now()

    result = await collection.update_one(
        {"_id": oid},
        {
            "$push": {"comments": comment},
            "$set": {"updated_at": utc_now()},
        },
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=404, detail="Ticket not found"
        )

    return comment


@app.patch("/tickets/{ticket_id}/status")
async def change_status(
    ticket_id: str,
    payload: StatusUpdate,
    request: Request,
):
    collection = get_collection(request)
    oid = parse_ticket_id(ticket_id)

    result = await collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": payload.status.value,
                "updated_at": utc_now(),
            }
        },
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=404, detail="Ticket not found"
        )

    ticket = await collection.find_one({"_id": oid})
    return serialize_ticket(ticket)
