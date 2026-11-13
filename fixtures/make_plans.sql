-- Input reproduction only. Synthetic deterministic rows, no expected diagnostic output.
CREATE TABLE customers(id integer PRIMARY KEY, parent_id integer NOT NULL, region text NOT NULL);
INSERT INTO customers SELECT i, 1+(i%40), CASE i%3 WHEN 0 THEN 'north' WHEN 1 THEN 'east' ELSE 'south' END FROM generate_series(1,200) i;
CREATE TABLE orders(id integer PRIMARY KEY, customer_id integer NOT NULL, amount integer NOT NULL);
INSERT INTO orders SELECT i, 1+(i%200), (i*37)%1000 FROM generate_series(1,4000) i;
CREATE INDEX orders_customer_idx ON orders(customer_id);
ANALYZE customers; ANALYZE orders;

-- BEFORE
SET max_parallel_workers_per_gather=0; SET jit=off; SET enable_nestloop=off; SET enable_mergejoin=off; SET enable_hashjoin=on; SET enable_hashagg=on;
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT c.region, p.region AS parent_region, count(*) AS n, sum(o.amount) AS amount
FROM customers c JOIN customers p ON p.id=c.parent_id JOIN orders o ON o.customer_id=c.id
WHERE c.region IN ('north','east') AND o.amount>200
GROUP BY c.region,p.region ORDER BY c.region,p.region;
-- AFTER
SET max_parallel_workers_per_gather=0; SET jit=off; SET enable_nestloop=on; SET enable_mergejoin=off; SET enable_hashjoin=off; SET enable_hashagg=off;
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT c.region, p.region AS parent_region, count(*) AS n, sum(o.amount) AS amount
FROM customers c JOIN customers p ON p.id=c.parent_id JOIN orders o ON o.customer_id=c.id
WHERE c.region IN ('north','east') AND o.amount>200
GROUP BY c.region,p.region ORDER BY c.region,p.region;
