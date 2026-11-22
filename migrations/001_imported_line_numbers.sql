-- Apply the owner's preference once to the original documentation import.
-- The source AI session must still have access to each active note.
CREATE TEMP TABLE imported_docs AS
SELECT n.id, n.title, n.revision, e.actor_id FROM notes n
JOIN projects p ON p.id=n.project_id AND p.archived=0
JOIN change_events e ON e.note_id=n.id AND e.action='created' AND e.actor_type='ai'
JOIN web_sessions s ON s.id=e.actor_id AND s.is_ai=1 AND s.revoked_at IS NULL AND s.expires_at>:now
JOIN session_note_access a ON a.note_id=n.id AND a.session_id=s.id AND a.allowed=1
WHERE n.status='active' AND n.line_numbers=1 AND n.id IN (
    '8b3ef580f73c9989596a5a6cd0912824',
    '8c6f86d5924d099ae04b290a9442a0e8',
    'bb76668eaaf5746d26ea46b2917beb19',
    '74c1cd1f2ddc296e1ba7e8326aff71d5',
    '4193bd935e2efa4abfcdc0f844f8db78',
    'a8c412a2035860e8cb4c0008929bdc6f',
    'c91b8b14d35d96c094a82c14ae53e2e9',
    '548c6ef41fb84b336796c9729b95c6f5',
    '8be62763415505e11d13094aa3c4148a',
    '0945dca6ae3c03549e8da30aacfa2123',
    '3b7361906e36d630d58feaf7ab6fe084',
    'cbbf592db198a3e6c436842367a98da4',
    '96a3c069ab6e635922a2a32c9174d78f',
    '2848d238e1a7352b9095f6cfc59eaa2c',
    '6147bdd479d2a6c7534014299b77fd39',
    '8273771558736e63f948975617892bbc',
    '63b0f22d56cce1526cf62289e44c00ab',
    '2ae279e4fe532633482c071b5d77827b',
    '84ff077cb65baa2dc95069a2e1c5e559',
    '18463066d7f86f30e21bfe53d2815b6e',
    '1c958d6daf8cddd8abcf6ece220959c7',
    '52ebcd6e7a5d8b338abf7d31d49a00f4',
    '99acfd112ea070d6c70430e982ce90b1',
    '9e86846877448cb5b5d237a756c36468',
    'e9043758bc445f4605d79cfa26220206',
    'aabf81f2395feffca6a15469622b7a3e',
    'df3a6ab492c1e0ddaa2d4310a78f6de9',
    '0717afbacb251caecb48474ec4f9d83f'
);
UPDATE notes SET line_numbers=0, revision=revision+1, updated_at=:now
WHERE id IN (SELECT id FROM imported_docs);
INSERT INTO change_events(id,actor_type,actor_id,note_id,action,occurred_at,revision,subject_title)
SELECT lower(hex(randomblob(16))), 'ai', actor_id, id, 'line numbers off', :now, revision+1, title
FROM imported_docs;
DROP TABLE imported_docs;
