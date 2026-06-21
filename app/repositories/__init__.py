"""Repository layer: database access isolated from business logic.

Repositories own an ``AsyncSession`` and expose query/persistence methods that
return ORM models or plain data. They contain no business rules and no HTTP
concerns, so services can be unit-tested against a fake repository.
"""