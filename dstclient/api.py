#
# Copyright 2021-2022 Basislager Services
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

"""Unified API for tickers and forums."""


__all__ = ("DerStandardAPI",)


import asyncio
import concurrent
import contextlib
import itertools
import json
import time
from typing import Any, AsyncContextManager, Optional, Union, cast
from urllib.parse import urlencode

from aiohttp import ClientSession

import dateutil.parser as dateparser

import pytz

from selenium.webdriver.common.by import By

from .dataclasses import Posting, Thread, User
from .utils import chromedriver


class DerStandardAPI:
    """Unified API for tickers and forums."""

    def __init__(self) -> None:
        self._cookies: Optional[dict[str, str]] = None

    def TURL(self, tail: str) -> str:
        """Construct an URL for a ticker API request."""
        return "https://www.derstandard.at/jetzt/api/" + tail

    def FURL(self, tail: str) -> str:
        """Construct an URL for a forum API request."""
        return "https://capi.ds.at/forum-serve-graphql/v1/" + tail

    def session(self) -> ClientSession:
        """Create a client session with credentials."""
        headers = {"content-type": "application/json"}
        return ClientSession(cookies=self._cookies, headers=headers)

    def _session_context(
        self, client_session: Optional[ClientSession]
    ) -> AsyncContextManager[ClientSession]:
        if client_session:
            return contextlib.nullcontext(client_session)
        return self.session()

    ###########################################################################
    # Ticker API                                                              #
    ###########################################################################
    async def get_ticker_threads(
        self,
        ticker_id: Union[int, str],
        *,
        client_session: Optional[ClientSession] = None,
    ) -> list[Thread]:
        """Get a list of thread IDs of a ticker."""
        url = self.TURL(f"redcontent?id={ticker_id}&ps=1000000")

        async with self._session_context(client_session) as session:
            async with session.get(url) as resp:
                return [
                    Thread(
                        thread_id=t["id"],
                        published=dateparser.parse(t["ctd"]).astimezone(pytz.utc),
                        ticker_id=int(ticker_id),
                        title=t.get("hl") or None,
                        message=t.get("cm") or None,
                        user=User(user_id=t["cid"], name=t["cn"]),
                        upvotes=t["vp"],
                        downvotes=t["vn"],
                    )
                    for t in (await resp.json())["rcs"]
                ]

    async def _get_thread_postings_page(
        self,
        ticker_id: Union[int, str],
        thread_id: Union[int, str],
        skip_to: Union[None, int, str] = None,
        *,
        client_session: Optional[ClientSession] = None,
    ) -> Any:
        """Get a single page of postings from a ticker thread."""
        url = self.TURL(f"postings?objectId={ticker_id}&redContentId={thread_id}")
        if skip_to:
            url += f"&skipToPostingId={skip_to}"

        async with self._session_context(client_session) as session:
            async with session.get(url) as resp:
                return await resp.json()

    async def get_thread_postings(
        self,
        ticker_id: Union[int, str],
        thread_id: Union[int, str],
        *,
        client_session: Optional[ClientSession] = None,
    ) -> list[Posting]:
        """Get all postings in a ticker thread."""
        postings = []
        page = await self._get_thread_postings_page(
            ticker_id,
            thread_id,
            client_session=client_session,
        )
        while page["p"]:
            postings.extend(page["p"])
            skip_to = page["p"][-1]["pid"]
            page = await self._get_thread_postings_page(
                ticker_id,
                thread_id,
                skip_to,
                client_session=client_session,
            )

        # Remove duplicates.
        postings = list({p["pid"]: p for p in postings}.values())
        return [
            Posting(
                posting_id=p["pid"],
                parent_id=p["ppid"],
                user=User(user_id=p["cid"], name=p["cn"]),
                thread_id=int(thread_id),
                published=dateparser.parse(p["cd"]).astimezone(pytz.utc),
                title=p.get("hl") or None,
                message=p.get("tx") or None,
                upvotes=p["vp"],
                downvotes=p["vn"],
            )
            for p in postings
        ]

    ###########################################################################
    # Forum API                                                               #
    ###########################################################################
    async def _get_forum_id(
        self,
        article_id: Union[int, str],
        *,
        client_session: Optional[ClientSession] = None,
    ) -> str:
        """Get the forum ID for an article."""
        query = {
            "variables": json.dumps(
                {"contextUri": f"https://www.derstandard.at/story/{article_id}"}
            ),
            "query": """
                query GetForumInfo($contextUri: String!) {
                    getForumByContextUri(contextUri: $contextUri) {
                        id
                        totalPostingCount
                    }
                }
            """,
        }
        url = self.FURL("?" + urlencode(query))

        async with self._session_context(client_session) as session:
            async with session.get(url) as resp:
                response = await resp.json()
                return cast(str, response["data"]["getForumByContextUri"]["id"])

    async def get_forum_postings(
        self,
        article_id: Union[int, str],
        *,
        client_session: Optional[ClientSession] = None,
    ) -> list[Posting]:
        """Get all postings in a forum."""

        def nodequery(n: int) -> str:
            """Create the recursive query to get replies."""
            if not n:
                return "id"
            return f"""
                id
                lifecycleStatus
                author {{id
                  name
                }}
                title
                text
                reactions {{
                  aggregated {{name value}}
                }}
                history {{
                  created
                }}
                rootPostingId
                replies {{{nodequery(n - 1)}}}
            """

        forum_id = await self._get_forum_id(article_id, client_session=client_session)

        # TODO: Allow 32 levels like the JS implementation.
        subquery = nodequery(18)
        query = {
            "variables": json.dumps({"id": forum_id, "first": 100_000}),
            "query": f"""
              query ThreadsByForumQuery($id: String!, $first: Int) {{
                getForumRootPostings(getForumRootPostingsParams: {{forumId: $id, first: $first}}) {{
                  edges {{
                    node {{
                      {subquery}
                    }}
                  }}
                }}
              }}
            """,
        }
        url = self.FURL("?" + urlencode(query))

        def linearize(edges: Any) -> Any:
            """Traverse and linearize the reply tree."""
            postings = [e for e in edges]
            for e in edges:
                postings += linearize(e["replies"])
            return postings

        async with self._session_context(client_session) as session:
            async with session.get(url) as resp:
                response = await resp.json()
                root = [
                    e["node"] for e in response["data"]["getForumRootPostings"]["edges"]
                ]
                raw_postings = linearize(root)

        # Convert to dataclass
        return [
            Posting(
                posting_id=p["id"],
                parent_id=None if p["id"] == p["rootPostingId"] else p["rootPostingId"],
                user=User(p["author"]["id"], p["author"]["name"]),
                thread_id=None,
                published=dateparser.parse(p["history"]["created"]),
                upvotes=p["reactions"]["aggregated"][0]["value"],
                downvotes=p["reactions"]["aggregated"][1]["value"],
                title=p["title"] or None,
                message=p["text"] or None,
            )
            for p in raw_postings
            if p["lifecycleStatus"] == "Published"
        ]

    ###########################################################################
    # Accept terms and conditions                                             #
    ###########################################################################
    async def update_cookies(self) -> None:
        """Update credentials and GDPR cookies."""
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            self._cookies = await loop.run_in_executor(pool, self._accept_conditions)

    def _accept_conditions(self, timeout: Optional[int] = None) -> dict[str, str]:
        """Accept terms and conditions and return necessary cookies.

        Cookies are in a format suitable for the aiohttp.ClientSession.
        """
        with chromedriver() as driver:
            driver.get("https://www.derstandard.at/consent/tcf/")
            it = itertools.count() if timeout is None else range(int(timeout + 0.5))
            for _ in it:
                # Find the correct iframe
                for element in driver.find_elements(By.TAG_NAME, "iframe"):
                    if element.get_attribute("title") == "SP Consent Message":
                        driver.switch_to.frame(element)
                        # Find the correct button and click it.
                        for button in driver.find_elements(By.TAG_NAME, "button"):
                            if button.get_attribute("title") == "Einverstanden":
                                button.click()
                                return {
                                    c["name"]: c["value"] for c in driver.get_cookies()
                                }
                    time.sleep(1)
            else:
                raise TimeoutError("accepting terms and conditions timed out")
