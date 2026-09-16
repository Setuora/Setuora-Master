# Master release checklist

- [ ] The Master application binds to loopback; the public HTTPS proxy exposes only `/api/v1` node endpoints.
- [ ] Master admin pages, Tally gateway, database, and backups are not public.
- [ ] Each franchise has a distinct active code and node credential.
- [ ] Duplicate and out-of-order events are handled without duplicate network transactions.
- [ ] A purchase, receive, sale, and sales return event reaches the central Tally queue as expected.
- [ ] A Tally outage leaves a pending or failed batch for review; accepted Lite events are retained.
- [ ] A Tally voucher is not reported as completed to operators solely because Master accepted its event.
- [ ] The legacy SFTP worker is stopped and no new public SFTP rule is installed.
- [ ] Verified SQLite backups and central Tally backup recovery have been tested.
