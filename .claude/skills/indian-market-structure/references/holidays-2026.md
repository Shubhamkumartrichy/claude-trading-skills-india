# NSE/BSE trading holidays 2026 (equity, equity derivatives)

Verified 31 Oct 2026 against Upstox's NSE holiday page and news reports of the 15 Jan closure. The
authoritative list is NSE "Market Timings & Holidays" (nseindia.com). Exchanges can add holidays at
short notice.

| # | Date | Day | Occasion |
|---|---|---|---|
| 1 | 15 Jan 2026 | Thu | Maharashtra municipal corporation elections (declared 12 Jan; expiries moved to 14 Jan) |
| 2 | 26 Jan 2026 | Mon | Republic Day |
| 3 | 03 Mar 2026 | Tue | Holi |
| 4 | 26 Mar 2026 | Thu | Shri Ram Navami |
| 5 | 31 Mar 2026 | Tue | Shri Mahavir Jayanti |
| 6 | 03 Apr 2026 | Fri | Good Friday |
| 7 | 14 Apr 2026 | Tue | Dr. Baba Saheb Ambedkar Jayanti |
| 8 | 01 May 2026 | Fri | Maharashtra Day |
| 9 | 28 May 2026 | Thu | Bakri Id |
| 10 | 26 Jun 2026 | Fri | Muharram |
| 11 | 14 Sep 2026 | Mon | Ganesh Chaturthi |
| 12 | 02 Oct 2026 | Fri | Mahatma Gandhi Jayanti |
| 13 | 20 Oct 2026 | Tue | Dussehra |
| 14 | 10 Nov 2026 | Tue | Diwali Balipratipada |
| 15 | 24 Nov 2026 | Tue | Prakash Gurpurb Sri Guru Nanak Dev |
| 16 | 25 Dec 2026 | Fri | Christmas |

Weekend holidays with no effect on trading: 15 Feb (Mahashivratri), 21 Mar (Id-ul-Fitr), 15 Aug
(Independence Day), and 8 Nov (Diwali Laxmi Pujan, which has a **Muhurat trading** session with
timings by circular).

Special live session: Sunday 1 Feb 2026, 09:15–15:00 (Union Budget).

Many 2026 holidays fall on **Tuesday**, the NSE expiry day. Those expiries move to the Monday before.
`fno-options-analysis/scripts/options.py expiries` applies this list automatically. For 2027 and later
it applies only fixed-date holidays (26 Jan, 1 May, 15 Aug, 2 Oct, 25 Dec) and flags the dates, so
update `HOLIDAYS` in that script when NSE publishes the next calendar (usually in December).
