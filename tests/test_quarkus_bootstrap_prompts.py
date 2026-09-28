"""Keep framework-managed REST startup distinct from the standalone solver."""
from vibesolve.agents.prompts import load_prompt


def test_integrator_delegates_rest_startup_to_quarkus():
    prompt = load_prompt("integrator")
    assert "Quarkus generates the REST server entry point" in prompt
    assert "Do not generate a custom Quarkus launcher" in prompt
    assert "quarkus.package.main-class" in prompt
    assert "exec-maven-plugin version 3.6.3" in prompt
    assert "Ensure there is a class with a main method that runs Quarkus" not in prompt


def test_review_and_repair_preserve_the_two_entry_point_contracts():
    for role in ["reviewer", "fixer"]:
        prompt = load_prompt(role)
        assert "QuarkusApplication" in prompt and "Quarkus.run(args)" in prompt
        assert "standalone solver Main" in prompt
