# OSWorld benchmark record

Each `<run>.jsonl` here holds one row per task a cloud run scored, written by `scripts/osworld-gcp`
(see `typesafe_computer_use/osworld/record.py` and [docs/osworld.md](../../docs/osworld.md)). Rows
are never edited; committing a run's file makes it part of the record.

## Deferred until a residential proxy is set up

OSWorld marks these 52 of its 369 OSWorld 1.0 tasks `"proxy": true`: they browse live sites that
block or vary for a datacenter IP. OSWorld's default provider is a residential proxy (DataImpulse);
without its credentials the runners leave the proxy off, for jev and Luna alike, so these tasks wait
until one is set up and turned on for both. Two of them ran before this was decided: 06fe7178 and
7a5a7856.

- chrome/06fe7178-4491-4589-810f-2e2bc9502122
- chrome/0d8b7de3-e8de-4d86-b9fd-dd2dce58a217
- chrome/121ba48f-9e17-48ce-9bc6-a4fb17a7ebba
- chrome/1704f00f-79e6-43a7-961b-cedd3724d5fd
- chrome/35253b65-1c19-4304-8aa4-6884b8218fc0
- chrome/368d9ba4-203c-40c1-9fa3-da2f1430ce63
- chrome/44ee5668-ecd5-4366-a6ce-c1c9b8d4e938
- chrome/47543840-672a-467d-80df-8f7c3b9788c9
- chrome/59155008-fe71-45ec-8a8f-dc35497b6aa8
- chrome/7a5a7856-f1b6-42a4-ade9-1ca81ca0f263
- chrome/7b6c7e24-c58a-49fc-a5bb-d57b80e5b4c3
- chrome/7f52cab9-535c-4835-ac8c-391ee64dc930
- chrome/82279c77-8fc6-46f6-9622-3ba96f61b477
- chrome/82bc8d6a-36eb-4d2d-8801-ef714fb1e55a
- chrome/9f3f70fc-5afc-4958-a7b7-3bb4fcb01805
- chrome/a96b564e-dbe9-42c3-9ccf-b4498073438a
- chrome/b070486d-e161-459b-aa2b-ef442d973b92
- chrome/b7895e80-f4d1-4648-bee0-4eb45a6f1fa8
- chrome/c1fa57f3-c3db-4596-8f09-020701085416
- chrome/cabb3bae-cccb-41bd-9f5d-0f3a9fecd825
- chrome/da46d875-6b82-4681-9284-653b0c7ae241
- chrome/e1e75309-3ddb-4d09-92ec-de869c928143
- chrome/f0b971a1-6831-4b9b-a50e-22a6e47f45ba
- chrome/f5d96daf-83a8-4c86-9686-bada31fc66ab
- chrome/f79439ad-3ee8-4f99-a518-0eb60e5652b0
- multi_apps/0e5303d4-8820-42f6-b18d-daf7e633de21
- multi_apps/22a4636f-8179-4357-8e87-d1743ece1f81
- multi_apps/236833a3-5704-47fc-888c-4f298f09f799
- multi_apps/26660ad1-6ebb-4f59-8cba-a8432dfe8d38
- multi_apps/3e3fc409-bff3-4905-bf16-c968eee3f807
- multi_apps/3f05f3b9-29ba-4b6b-95aa-2204697ffc06
- multi_apps/42d25c08-fb87-4927-8b65-93631280a26f
- multi_apps/46407397-a7d5-4c6b-92c6-dbe038b1457b
- multi_apps/4e9f0faf-2ecc-4ae8-a804-28c9a75d1ddc
- multi_apps/58565672-7bfe-48ab-b828-db349231de6b
- multi_apps/5990457f-2adb-467b-a4af-5c857c92d762
- multi_apps/67890eb6-6ce5-4c00-9e3d-fb4972699b06
- multi_apps/74d5859f-ed66-4d3e-aa0e-93d7a592ce41
- multi_apps/78aed49a-a710-4321-a793-b611a7c5b56b
- multi_apps/897e3b53-5d4d-444b-85cb-2cdc8a97d903
- multi_apps/a0b9dc9c-fc07-4a88-8c5d-5e3ecad91bcb
- multi_apps/b52b40a5-ad70-4c53-b5b0-5650a8387052
- multi_apps/d1acdb87-bb67-4f30-84aa-990e56a09c92
- multi_apps/da52d699-e8d2-4dc5-9191-a2199e0b6a9b
- multi_apps/da922383-bfa4-4cd3-bbad-6bebab3d7742
- multi_apps/dd60633f-2c72-42ba-8547-6f2c8cb0fdb0
- multi_apps/df67aebb-fb3a-44fd-b75b-51b6012df509
- multi_apps/e135df7c-7687-4ac0-a5f0-76b74438b53e
- multi_apps/e2392362-125e-4f76-a2ee-524b183a3412
- multi_apps/f8cfa149-d1c1-4215-8dac-4a0932bad3c2
- vs_code/4e60007a-f5be-4bfc-9723-c39affa0a6d3
- vs_code/eabc805a-bfcf-4460-b250-ac92135819f6
