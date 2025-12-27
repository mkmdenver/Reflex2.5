--
-- PostgreSQL database dump
--

\restrict fOlzMAdxcccIAgdwbBl6V1EwrEF7BF23unYbNbb7ChOcEYylBcvyQH1c8R1yqC6

-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: tick_data; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.tick_data (
    symbol text NOT NULL,
    "timestamp" timestamp with time zone NOT NULL,
    sip_timestamp bigint,
    participant_timestamp bigint,
    trf_timestamp bigint,
    price numeric(18,6) NOT NULL,
    size integer NOT NULL,
    exchange integer,
    conditions integer[],
    tape integer,
    trade_id text
);


ALTER TABLE public.tick_data OWNER TO postgres;

--
-- Name: tick_data tick_data_symbol_timestamp_trade_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.tick_data
    ADD CONSTRAINT tick_data_symbol_timestamp_trade_id_key UNIQUE (symbol, "timestamp", trade_id);


--
-- Name: tick_data tick_price_positive_chk; Type: CHECK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE public.tick_data
    ADD CONSTRAINT tick_price_positive_chk CHECK ((price > (0)::numeric)) NOT VALID;


--
-- Name: tick_data tick_size_positive_chk; Type: CHECK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE public.tick_data
    ADD CONSTRAINT tick_size_positive_chk CHECK ((size > 0)) NOT VALID;


--
-- Name: tick_data tick_ts_after_2000_chk; Type: CHECK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE public.tick_data
    ADD CONSTRAINT tick_ts_after_2000_chk CHECK (("timestamp" >= '2000-01-01'::date)) NOT VALID;


--
-- Name: tick_data tick_ts_not_future_chk; Type: CHECK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE public.tick_data
    ADD CONSTRAINT tick_ts_not_future_chk CHECK (("timestamp" < (now() + '1 day'::interval))) NOT VALID;


--
-- Name: ix_tick_symbol_ts; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_tick_symbol_ts ON public.tick_data USING btree (symbol, "timestamp");


--
-- Name: tick_data_timestamp_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX tick_data_timestamp_idx ON public.tick_data USING btree ("timestamp" DESC);


--
-- Name: tick_data ts_insert_blocker; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER ts_insert_blocker BEFORE INSERT ON public.tick_data FOR EACH ROW EXECUTE FUNCTION _timescaledb_functions.insert_blocker();


--
-- Name: TABLE tick_data; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tick_data TO reflex;


--
-- PostgreSQL database dump complete
--

\unrestrict fOlzMAdxcccIAgdwbBl6V1EwrEF7BF23unYbNbb7ChOcEYylBcvyQH1c8R1yqC6

