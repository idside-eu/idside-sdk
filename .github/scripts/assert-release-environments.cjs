"use strict";

function selectedEnvironments(packages) {
  if (packages === "both") return ["npm", "pypi"];
  if (packages === "npm" || packages === "pypi") return [packages];
  throw new Error(`Unknown package selection: ${packages}`);
}

async function assertProtectedReleaseEnvironments({ packages, getEnvironment }) {
  for (const name of selectedEnvironments(packages)) {
    let environment;
    try {
      environment = await getEnvironment(name);
    } catch (error) {
      if (error?.status === 404) {
        throw new Error(
          `Release environment ${name} does not exist. Create and protect it before publishing.`,
        );
      }

      const status = error?.status ? ` (HTTP ${error.status})` : "";
      throw new Error(
        `Release environment ${name} could not be verified through the GitHub API${status}.`,
      );
    }

    const reviewerRule = environment.protection_rules?.find(
      (rule) => rule.type === "required_reviewers",
    );
    if (!reviewerRule || !Array.isArray(reviewerRule.reviewers) || reviewerRule.reviewers.length === 0) {
      throw new Error(
        `Release environment ${name} has no required reviewer. Publishing remains disabled.`,
      );
    }
  }
}

module.exports = { assertProtectedReleaseEnvironments, selectedEnvironments };
