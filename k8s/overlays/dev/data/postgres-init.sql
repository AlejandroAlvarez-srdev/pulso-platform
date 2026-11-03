-- Databases for the services that persist to Postgres (today only orders-api, via Prisma).
-- Shared by docker-compose and the dev overlay; runs once, on an empty data directory.
CREATE DATABASE orders;
