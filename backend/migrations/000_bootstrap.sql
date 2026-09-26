CREATE ROLE glyphuser WITH LOGIN PASSWORD 'glyphpass';
ALTER ROLE glyphuser WITH SUPERUSER;
CREATE DATABASE glyphscholar OWNER glyphuser;