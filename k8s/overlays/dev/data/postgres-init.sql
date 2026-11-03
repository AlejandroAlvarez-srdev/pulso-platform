-- One database per service that persists state. Shared by docker-compose and the dev overlay.
-- Runs once, on first boot of an empty data directory.
CREATE DATABASE orders;
CREATE DATABASE saga;
CREATE DATABASE support;
