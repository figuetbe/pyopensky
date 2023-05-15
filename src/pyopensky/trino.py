from __future__ import annotations

import functools
import time
from datetime import timedelta
from multiprocessing.pool import ThreadPool
from operator import or_
from typing import Any, Iterable, Tuple, TypedDict, cast

import requests
from sqlalchemy import (
    Connection,
    CursorResult,
    Engine,
    Select,
    TextClause,
    create_engine,
    select,
)
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.expression import text
from tqdm import tqdm
from trino.auth import JWTAuthentication, OAuth2Authentication
from trino.sqlalchemy import URL

import pandas as pd

from .config import password, username
from .schema import FlightsData4
from .time import timelike, to_datetime


class Token(TypedDict):
    access_token: str


class Trino:
    def token(self, **kwargs: Any) -> None | Token:
        if username is None or password is None:
            return None
        result = requests.post(
            "https://auth.opensky-network.org/auth/realms/"
            "opensky-network/protocol/openid-connect/token",
            data={
                "client_id": "trino-client",
                "grant_type": "password",
                "username": username,
                "password": password,
            },
            **kwargs,
        )
        result.raise_for_status()
        return cast(Token, result.json())

    def engine(self) -> Engine:
        token = self.token()
        engine = create_engine(
            URL(
                "trino.opensky-network.org",
                port=443,
                user=username,
                catalog="minio",
                schema="osky",
            ),
            connect_args=dict(
                auth=JWTAuthentication(token["access_token"])
                if token is not None
                else OAuth2Authentication(),
                http_scheme="https",
            ),
        )
        return engine

    def connect(self) -> Connection:
        return self.engine().connect()

    def query(self, query: str | TextClause | Select[Any]) -> pd.DataFrame:
        exec_kw = dict(stream_results=True)  # not sure this option is necessary
        if isinstance(query, str):
            query = text(query)
        with self.connect().execution_options(**exec_kw) as connect:
            # There are steps here, that will not appear in the progress bar
            # but that you can check on https://trino.opensky-network.org/ui/
            return pd.concat(self.process_result(connect.execute(query)))

    def process_result(
        self,
        res: CursorResult[Any],
        batch_size: int = 50_000,
    ) -> Iterable[pd.DataFrame]:
        pool = ThreadPool(processes=1)
        async_result = pool.apply_async(res.fetchmany, (batch_size,))
        percentage = 0

        with tqdm(unit="%", unit_scale=True) as processing_bar:
            while not async_result.ready():
                processing_bar.set_description(res.cursor.stats["state"])
                increment = res.cursor.stats["progressPercentage"] - percentage
                percentage = res.cursor.stats["progressPercentage"]
                processing_bar.update(increment)

                time.sleep(0.1)

            if res.cursor is not None:
                increment = res.cursor.stats["progressPercentage"] - percentage
                percentage = res.cursor.stats["progressPercentage"]
                processing_bar.set_description(res.cursor.stats["state"])

        with tqdm(
            unit="lines", unit_scale=True, desc="DOWNLOAD"
        ) as download_bar:
            sequence_rows = async_result.get()
            download_bar.update(len(sequence_rows))
            yield pd.DataFrame.from_records(sequence_rows, columns=res.keys())

            while len(sequence_rows) == batch_size:
                sequence_rows = res.fetchmany(batch_size)
                download_bar.update(len(sequence_rows))
                yield pd.DataFrame.from_records(
                    sequence_rows, columns=res.keys()
                )

    ## Specific queries

    def stmt_where_str(
        self,
        stmt: Select[Any],
        value: None | str | list[str],
        *attr: InstrumentedAttribute[str],
    ) -> Select[Any]:
        if len(attr) == 0:
            return stmt
        if isinstance(value, str):
            if value.find("%") >= 0 or value.find("_") >= 0:
                like = functools.reduce(or_, (a.like(value) for a in attr))
                stmt = stmt.where(like)
            else:
                equal = functools.reduce(or_, (a == value for a in attr))
                stmt = stmt.where(equal)
        elif isinstance(value, Iterable):
            is_in = functools.reduce(or_, (a.in_(list(value)) for a in attr))
            stmt = stmt.where(is_in)
        # stmt = stmt.where(attr.in_(list(value)))
        return stmt

    def flightlist(
        self,
        start: timelike,
        stop: None | timelike = None,
        *args: Any,  # more reasonable to be explicit about arguments
        departure_airport: None | str | list[str] = None,
        arrival_airport: None | str | list[str] = None,
        airport: None | str | list[str] = None,
        callsign: None | str | list[str] = None,
        icao24: None | str | list[str] = None,
        cached: bool = True,
        compress: bool = False,
        limit: None | int = None,
    ) -> pd.DataFrame:
        """Lists flights departing or arriving at a given airport.

        You may pass requests based on time ranges, callsigns, aircraft, areas,
        serial numbers for receivers, or airports of departure or arrival.

        The method builds appropriate SQL requests, caches results and formats
        data into a proper pandas DataFrame. Requests are split by hour (by
        default) in case the connection fails.

        :param start: a string (default to UTC), epoch or datetime (native
            Python or pandas)
        :param stop: a string (default to UTC), epoch or datetime (native Python
            or pandas), *by default, one day after start*

        More arguments to filter resulting data:

        :param departure_airport: a string for the ICAO identifier of the
            airport. Selects flights departing from the airport between the two
            timestamps;
        :param arrival_airport: a string for the ICAO identifier of the airport.
            Selects flights arriving at the airport between the two timestamps;
        :param airport: a string for the ICAO identifier of the airport. Selects
            flights departing from or arriving at the airport between the two
            timestamps;
        :param callsign: a string or a list of strings (wildcards
            accepted, _ for any character, % for any sequence of characters);
        :param icao24: a string or a list of strings identifying the transponder
            code of the aircraft;

        .. warning::

            - If both departure_airport and arrival_airport are set, requested
              timestamps match the arrival time;
            - If airport is set, ``departure_airport`` and ``arrival_airport``
              cannot be specified (a RuntimeException is raised).

        **Useful options for debug**

        :param cached: (default: True) switch to False to force a new request to
            the database regardless of the cached files. This option also
            deletes previous cache files;
        :param compress: (default: False) compress cache files. Reduces disk
            space occupied at the expense of slightly increased time
            to load.
        :param limit: maximum number of records requested, LIMIT keyword in SQL.

        """

        start = to_datetime(start)
        if stop is not None:
            stop = to_datetime(stop)
        else:
            stop = start + timedelta(days=1)

        stmt = select(FlightsData4).with_only_columns(
            FlightsData4.icao24,
            FlightsData4.firstseen,
            FlightsData4.estdepartureairport,
            FlightsData4.lastseen,
            FlightsData4.estarrivalairport,
            FlightsData4.callsign,
            FlightsData4.day,
        )

        stmt = self.stmt_where_str(stmt, icao24, FlightsData4.icao24)
        stmt = self.stmt_where_str(stmt, callsign, FlightsData4.callsign)
        stmt = self.stmt_where_str(
            stmt,
            departure_airport,
            FlightsData4.estdepartureairport,
        )
        stmt = self.stmt_where_str(
            stmt,
            arrival_airport,
            FlightsData4.estarrivalairport,
        )
        if airport is not None and arrival_airport is not None:
            raise RuntimeError("airport may not be set if arrival_airport is")
        if airport is not None and departure_airport is not None:
            raise RuntimeError("airport may not be set if departure_airport is")
        stmt = self.stmt_where_str(
            stmt,
            arrival_airport,
            FlightsData4.estdepartureairport,
            FlightsData4.estarrivalairport,
        )

        if departure_airport is not None:
            stmt = stmt.where(
                FlightsData4.firstseen >= start,
                FlightsData4.firstseen <= stop,
                FlightsData4.day >= pd.to_datetime(start).floor("1d"),
                FlightsData4.day < pd.to_datetime(stop).ceil("1d"),
            )
        else:
            stmt = stmt.where(
                FlightsData4.lastseen >= start,
                FlightsData4.lastseen <= stop,
                FlightsData4.day >= pd.to_datetime(start).floor("1d"),
                FlightsData4.day < pd.to_datetime(stop).ceil("1d"),
            )

        if limit is not None:
            stmt = stmt.limit(limit)

        res = self.query(stmt)

        if res.shape[0] == 0:
            return None

        return res.rename(
            columns=dict(
                estarrivalairport="arrival",
                estdepartureairport="departure",
            )
        )