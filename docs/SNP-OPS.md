# SNP/1 operation catalogue

Generated from `engine/sarness_engine/snp/ops.json` (normative). `mode` is defined in SNP.md §6.1. Argument names are the keys of `args`; `body` is the request-body map. The `HTTP route` column is informative.

| Operation | Mode | Arguments | HTTP route (informative) |
|---|---|---|---|
| `chat.conversation.list` | unary | `q`, `project_id` | GET `/v1/chat/conversations` |
| `chat.conversation.create` | unary | `body` | POST `/v1/chat/conversations` |
| `chat.conversation.get` | unary | `cid` | GET `/v1/chat/conversations/{cid}` |
| `chat.conversation.update` | unary | `cid`, `body` | PATCH `/v1/chat/conversations/{cid}` |
| `chat.conversation.delete` | unary | `cid` | DELETE `/v1/chat/conversations/{cid}` |
| `chat.message.delete` | unary | `cid`, `mid` | DELETE `/v1/chat/conversations/{cid}/messages/{mid}` |
| `chat.generate` | stream | `cid`, `body` | POST `/v1/chat/conversations/{cid}/generate` |
| `chat.stop` | unary | `cid` | POST `/v1/chat/conversations/{cid}/stop` |
| `chat.conversation.export` | unary | `cid`, `format` | GET `/v1/chat/conversations/{cid}/export` |
| `chat.conversation.import` | unary | `body` | POST `/v1/chat/import` |
| `agent.models` | unary | — | GET `/v1/agent/models` |
| `agent.completion` | stream | `body` | POST `/v1/agent/completions` |
| `job.submit` | unary | `body` | POST `/v1/jobs` |
| `job.list` | unary | `status`, `kind`, `limit` | GET `/v1/jobs` |
| `job.get` | unary | `job_id` | GET `/v1/jobs/{job_id}` |
| `job.cancel` | unary | `job_id` | POST `/v1/jobs/{job_id}/cancel` |
| `job.retry` | unary | `job_id`, `body` | POST `/v1/jobs/{job_id}/retry` |
| `job.clear_finished` | unary | — | DELETE `/v1/jobs/finished` |
| `workflow.nodes` | unary | — | GET `/v1/nodes` |
| `workflow.validate` | unary | `body` | POST `/v1/workflows/validate` |
| `workflow.compile` | unary | `body` | POST `/v1/workflows/compile` |
| `workflow.simplify` | unary | `body` | POST `/v1/workflows/simplify` |
| `workflow.list` | unary | `kind`, `presets` | GET `/v1/workflows` |
| `workflow.create` | unary | `body` | POST `/v1/workflows` |
| `workflow.get` | unary | `wid` | GET `/v1/workflows/{wid}` |
| `workflow.update` | unary | `wid`, `body` | PUT `/v1/workflows/{wid}` |
| `workflow.delete` | unary | `wid` | DELETE `/v1/workflows/{wid}` |
| `project.list` | unary | — | GET `/v1/projects` |
| `project.create` | unary | `body` | POST `/v1/projects` |
| `project.update` | unary | `pid`, `body` | PATCH `/v1/projects/{pid}` |
| `project.delete` | unary | `pid` | DELETE `/v1/projects/{pid}` |
| `folder.create` | unary | `body` | POST `/v1/folders` |
| `folder.update` | unary | `fid`, `body` | PATCH `/v1/folders/{fid}` |
| `folder.delete` | unary | `fid` | DELETE `/v1/folders/{fid}` |
| `generation.list` | unary | `type`, `project_id`, `folder_id`, `q`, `model_id`, `favorite`, `tag`, `collection_id`, `since`, `until`, `before`, `limit`, `trash`, `job_id` | GET `/v1/generations` |
| `generation.get` | unary | `gid` | GET `/v1/generations/{gid}` |
| `generation.update` | unary | `gid`, `body` | PATCH `/v1/generations/{gid}` |
| `generation.bulk` | unary | `body` | POST `/v1/generations/bulk` |
| `generation.delete` | unary | `gid`, `permanent` | DELETE `/v1/generations/{gid}` |
| `generation.rerun` | unary | `gid`, `body` | POST `/v1/generations/{gid}/rerun` |
| `generation.metadata` | unary | `gid` | GET `/v1/generations/{gid}/metadata` |
| `tag.list` | unary | — | GET `/v1/tags` |
| `library.models` | unary | — | GET `/v1/library/models` |
| `collection.list` | unary | — | GET `/v1/collections` |
| `collection.create` | unary | `body` | POST `/v1/collections` |
| `collection.rename` | unary | `cid`, `body` | PATCH `/v1/collections/{cid}` |
| `collection.delete` | unary | `cid` | DELETE `/v1/collections/{cid}` |
| `collection.add_items` | unary | `cid`, `body` | POST `/v1/collections/{cid}/items` |
| `collection.remove_item` | unary | `cid`, `gid` | DELETE `/v1/collections/{cid}/items/{gid}` |
| `file.get` | download | `fid`, `exp`, `sig` | GET `/v1/files/{fid}` |
| `file.sign` | unary | `fid` | POST `/v1/files/{fid}/sign` |
| `file.upload` | upload | — | POST `/v1/uploads` |
| `model.list` | unary | `category` | GET `/v1/models` |
| `model.rescan` | unary | — | POST `/v1/models/rescan` |
| `model.delete` | unary | `model_id` | DELETE `/v1/models/{model_id:path}` |
| `model.catalog` | unary | — | GET `/v1/models/catalog` |
| `model.presets` | unary | — | GET `/v1/models/presets` |
| `model.disk` | unary | — | GET `/v1/models/disk` |
| `model.install` | unary | `body` | POST `/v1/models/install` |
| `download.list` | unary | — | GET `/v1/downloads` |
| `download.action` | unary | `did`, `action` | POST `/v1/downloads/{did}/{action}` |
| `hf.search` | unary | `q`, `kind` | GET `/v1/hf/search` |
| `hf.inspect` | unary | `repo` | GET `/v1/hf/inspect` |
| `system.health` | unary | — | GET `/v1/health` |
| `system.info` | unary | — | GET `/v1/system` |
| `settings.get` | unary | — | GET `/v1/settings` |
| `settings.patch` | unary | `patch` | PATCH `/v1/settings` |
| `gateway.status` | unary | — | GET `/v1/gateway` |
| `gateway.refresh` | unary | — | POST `/v1/gateway/refresh` |
| `runtime.list` | unary | — | GET `/v1/runtimes` |
| `runtime.llama.install` | unary | `body` | POST `/v1/runtimes/llama/install` |
| `worker.release` | unary | — | POST `/v1/worker/release` |
| `worker.stop` | unary | — | POST `/v1/worker/stop` |
| `llm.status` | unary | — | GET `/v1/llm/status` |
| `llm.models` | unary | — | GET `/v1/llm/models` |
| `llm.load` | unary | `body` | POST `/v1/llm/load` |
| `llm.unload` | unary | — | POST `/v1/llm/unload` |
| `llm.endpoint.refresh` | unary | `endpoint_id` | POST `/v1/llm/endpoints/{endpoint_id}/refresh` |
| `event.subscribe` | subscribe | — | GET `/v1/events` |
| `experiment.list` | unary | — | GET `/v1/experiments` |
| `experiment.get` | unary | `eid` | GET `/v1/experiments/{eid}` |
| `experiment.document.list` | unary | `eid` | GET `/v1/experiments/{eid}/documents` |
| `experiment.document.create` | unary | `eid`, `body` | POST `/v1/experiments/{eid}/documents` |
| `experiment.document.get` | unary | `eid`, `doc_id` | GET `/v1/experiments/{eid}/documents/{doc_id}` |
| `experiment.document.update` | unary | `eid`, `doc_id`, `body` | PUT `/v1/experiments/{eid}/documents/{doc_id}` |
| `experiment.document.duplicate` | unary | `eid`, `doc_id`, `body` | POST `/v1/experiments/{eid}/documents/{doc_id}/duplicate` |
| `experiment.document.delete` | unary | `eid`, `doc_id` | DELETE `/v1/experiments/{eid}/documents/{doc_id}` |
| `paintbrush.status` | unary | — | GET `/v1/experiments/paintbrush/status` |
| `paintbrush.render` | unary | `body` | POST `/v1/experiments/paintbrush/render` |
| `paintbrush.cancel` | unary | `body` | POST `/v1/experiments/paintbrush/cancel` |
| `paintbrush.warmup` | unary | `body` | POST `/v1/experiments/paintbrush/warmup` |
| `paintbrush.release` | unary | `body` | POST `/v1/experiments/paintbrush/release` |
| `paintbrush.install` | unary | `body` | POST `/v1/experiments/paintbrush/install` |
| `paintbrush.install_cancel` | unary | — | POST `/v1/experiments/paintbrush/install/cancel` |
| `paintbrush.render.save` | unary | `render_id`, `body` | POST `/v1/experiments/paintbrush/renders/{render_id}/save` |
| `paintbrush.render.export` | unary | `render_id` | POST `/v1/experiments/paintbrush/renders/{render_id}/export` |
| `paintbrush.mask.export` | unary | `body` | POST `/v1/experiments/paintbrush/mask/export` |
