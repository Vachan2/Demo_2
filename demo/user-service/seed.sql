-- Seed data for Demo 2 user-service
CREATE TABLE IF NOT EXISTS users (
    id         SERIAL PRIMARY KEY,
    username   VARCHAR(64)  NOT NULL UNIQUE,
    email      VARCHAR(128) NOT NULL UNIQUE,
    full_name  VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

INSERT INTO users (username, email, full_name) VALUES
  ('alice',   'alice@example.com',   'Alice Anderson'),
  ('bob',     'bob@example.com',     'Bob Brown'),
  ('carol',   'carol@example.com',   'Carol Clark'),
  ('dave',    'dave@example.com',    'Dave Davis'),
  ('eve',     'eve@example.com',     'Eve Evans'),
  ('frank',   'frank@example.com',   'Frank Foster'),
  ('grace',   'grace@example.com',   'Grace Garcia'),
  ('henry',   'henry@example.com',   'Henry Harris'),
  ('iris',    'iris@example.com',    'Iris Ingram'),
  ('james',   'james@example.com',   'James Johnson'),
  ('kate',    'kate@example.com',    'Kate King'),
  ('liam',    'liam@example.com',    'Liam Lewis'),
  ('mia',     'mia@example.com',     'Mia Moore'),
  ('noah',    'noah@example.com',    'Noah Nelson'),
  ('olivia',  'olivia@example.com',  'Olivia Owen'),
  ('peter',   'peter@example.com',   'Peter Parker'),
  ('quinn',   'quinn@example.com',   'Quinn Quinn'),
  ('rose',    'rose@example.com',    'Rose Rogers'),
  ('sam',     'sam@example.com',     'Sam Smith'),
  ('tara',    'tara@example.com',    'Tara Turner')
ON CONFLICT DO NOTHING;
