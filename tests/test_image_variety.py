import json
import sqlite3
import unittest
from unittest.mock import Mock

from worker.conversation import respond


class ImageVarietyTests(unittest.TestCase):
    def test_planner_receives_three_recent_images_and_preserves_constraints(self):
        call = Mock(side_effect=[
            'Here is another picture.',
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The character smiles in a medium shot.","video_line":null}',
        ])
        respond({'message': 'Another picture', 'recent_image_descriptions':
                 ['oldest', 'close-up', None, 'profile', 'wide shot']},
                'chosen', call, Mock(), mode='nsfw')
        messages = call.call_args_list[1].kwargs['messages']
        self.assertEqual(json.loads(messages[1]['content'])['recent_image_descriptions'],
                         ['close-up', 'profile', 'wide shot'])
        self.assertIn('Explicit user instructions and required visual outcomes always override variety',
                      messages[0]['content'])
        self.assertIn('Apply this rule only to images', messages[0]['content'])

    def test_history_query_excludes_failed_other_character_and_other_context(self):
        from pathlib import Path
        import ast
        source = Path(__file__).resolve().parents[1] / 'vps' / 'app.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        query = next(node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and node.value.startswith("SELECT payload FROM jobs WHERE job_type='character_image_edit'"))
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE jobs (id INTEGER, job_type TEXT, status TEXT, context_id INTEGER, payload TEXT)')
        for index, status, context, character in [(1,'done',7,1),(2,'done',7,1),
                (3,'failed',7,1),(4,'done',8,1),(5,'done',7,2),
                (6,'done',7,1),(7,'done',7,1),(10,'done',7,1)]:
            conn.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                         (index,'character_image_edit',status,context,
                          json.dumps({'character_id':character,'prompt':str(index)})))
        rows = conn.execute(query, (9,7,1)).fetchall()
        self.assertEqual([json.loads(row[0])['prompt'] for row in reversed(rows)], ['2','6','7'])
        conn.close()
