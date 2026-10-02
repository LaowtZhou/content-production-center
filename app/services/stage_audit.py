"""Read-only projections of persisted stage callbacks and registered media."""
import hashlib
import json
from pathlib import Path

from app.config import get_setting
from app.services import production_service


def audit(task_id, stage_key):
    task = production_service.get_task(task_id)
    if not task:
        raise ValueError('任务不存在')
    definition = production_service._stage_definition(stage_key)
    attempts = []
    for row in task['stages']:
        if row['stage_key'] != stage_key:
            continue
        result = json.loads(row['result_json'] or '{}')
        attempts.append({
            'id': row['id'], 'attempt': row['attempt'], 'status': row['status'],
            'started_at': row['started_at'], 'completed_at': row['completed_at'],
            'role': row['role_name'], 'error': row['error_detail'],
            'summary': result.get('summary'), 'decision': result.get('decision'),
            'return_to_stage': result.get('return_to_stage'),
            'model': result.get('model'), 'reasoning_effort': result.get('reasoning_effort'),
            'used_skills': result.get('used_skills') or [],
            'outputs': json.loads(row['output_paths_json'] or '[]'),
        })
    return {'label': definition['label'], 'attempts': list(reversed(attempts)),
            'images': media(task) if stage_key == 'editor' else []}


def media(task):
    root_value = get_setting('content_vault_dir', '').strip()
    if not root_value:
        return []
    root = Path(root_value).resolve()
    paths = []
    for row in task['stages']:
        paths.extend(json.loads(row['output_paths_json'] or '[]'))
    for item in json.loads(task['artifact_manifest_json'] or '[]'):
        paths.append(item if isinstance(item, str) else item.get('path', ''))
    result = {}
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
            continue
        key = hashlib.sha256(str(path).encode('utf-8')).hexdigest()
        result[key] = {'id': key, 'name': path.name, 'path': str(path), 'exists': path.is_file(),
                       'url': f"/api/production/tasks/{task['id']}/images/{key}"}
    return list(result.values())


def image_path(task_id, image_id):
    task = production_service.get_task(task_id)
    if task:
        for item in media(task):
            if item['id'] == image_id and item['exists']:
                return Path(item['path'])
    raise ValueError('图片不存在或未登记到本任务')
