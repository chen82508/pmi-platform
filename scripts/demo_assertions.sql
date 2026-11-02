-- Fixture-demo acceptance check, shared by CI (`demo-e2e`) and `just e2e`.
-- Prints each demo index's series, then fails unless every one published a
-- full 15-point scored history and every evaluation came from the fixture
-- provider (nothing silently fell back to the stub).
SELECT d.index_id, count(s.score) AS points, round(min(s.score), 2) AS min,
       round(max(s.score), 2) AS max
  FROM ts_index_scores s
  JOIN core_index_definitions d ON d.id = s.index_definition_id
 GROUP BY 1 ORDER BY 1;

DO $$
BEGIN
  IF (SELECT count(DISTINCT index_definition_id) FROM ts_index_scores
       WHERE score IS NOT NULL) < 3
     OR EXISTS (SELECT 1 FROM ts_index_scores
                 GROUP BY index_definition_id HAVING count(score) < 15) THEN
    RAISE EXCEPTION 'demo indices did not all publish 15 scored points';
  END IF;
  IF EXISTS (SELECT 1 FROM audit_evaluations WHERE model_id <> 'fixture/labels') THEN
    RAISE EXCEPTION 'an evaluation bypassed the fixture provider';
  END IF;
  RAISE NOTICE 'demo e2e: OK';
END $$;
