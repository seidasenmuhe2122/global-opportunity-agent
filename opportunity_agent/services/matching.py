import re
from typing import Any


def _items(value):
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[,;\n]+', value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _norm(value):
    return {re.sub(r'\s+', ' ', item).casefold() for item in _items(value)}


def _text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _terms(value):
    stop_words = {
        'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'degree', 'experience',
        'for', 'in', 'is', 'of', 'or', 'qualification', 'qualifications', 'required',
        'the', 'to', 'with', 'years',
    }
    return {
        term.casefold()
        for term in re.findall(r'[A-Za-z0-9]+', ' '.join(_items(value)))
        if len(term) > 1 and term.casefold() not in stop_words
    }


def _threshold(value):
    try:
        return min(100, max(0, int(value)))
    except (TypeError, ValueError):
        return 75


def _generic_requirement_assessment(user, requirements):
    conflicts = []
    unconfirmed = []
    profile_country = _text(user.get('current_country')).casefold()
    profile_education = ' '.join(filter(None, (
        _text(user.get('degree')),
        _text(user.get('education')),
        ' '.join(_items(user.get('certifications'))),
    ))).casefold()
    profile_experience = _text(user.get('work_experience')).casefold()

    for clause in re.split(r'[\n;]+|(?<=[.!?])\s+', _text(requirements)):
        clause = clause.strip()
        if not clause:
            continue
        normalized = clause.casefold()
        if re.search(r'\b(?:citizen|citizenship|national|nationality|enrolled|student|age|aged)\b', normalized):
            unconfirmed.append(clause)
            continue

        residence = re.search(
            r'\b(?:must|only|eligible|required|requires?)\b.*\b(?:reside|resident|live|located)\b'
            r'.{0,30}?\b(?:in|of)\s+([A-Za-z][A-Za-z .-]+)',
            clause,
            re.I,
        )
        if residence:
            required_country = re.split(r'\b(?:and|or|who|with|for|to)\b|[,.;]', residence.group(1), maxsplit=1, flags=re.I)[0].strip().casefold()
            if not profile_country:
                unconfirmed.append(clause)
            elif required_country and profile_country != required_country:
                conflicts.append(clause)
            continue

        degree_level = re.search(
            r'\b(?:high school|secondary school|bachelor(?:\'s)?|bsc|master(?:\'s)?|msc|ph\.?d\.?|doctorate)\b',
            normalized,
        )
        if degree_level and re.search(r'\b(?:must|minimum|at least|required|requires?)\b', normalized):
            level = degree_level.group(0).replace("'s", '').replace('.', '')
            levels = {
                'high school': 1,
                'secondary school': 1,
                'bachelor': 2,
                'bsc': 2,
                'master': 3,
                'msc': 3,
                'phd': 4,
                'doctorate': 4,
            }
            required_level = levels.get(level)
            profile_levels = [
                rank for name, rank in levels.items()
                if re.search(r'\b' + re.escape(name) + r'\b', profile_education)
            ]
            if required_level is None or not profile_levels:
                unconfirmed.append(clause)
            elif max(profile_levels) < required_level:
                conflicts.append(clause)
            continue

        experience = re.search(
            r'\b(?:at least|minimum of|min\.?)\s+(\d+)\+?\s+years?\b',
            normalized,
        )
        if experience:
            years_in_profile = re.search(r'\b(\d+)\+?\s+years?\b', profile_experience)
            if not years_in_profile:
                unconfirmed.append(clause)
            elif int(years_in_profile.group(1)) < int(experience.group(1)):
                conflicts.append(clause)
            continue

        if re.search(r'\b(?:must|only|eligible|required|requires?|minimum|at least)\b', normalized) and not re.search(
            r'\b(?:submit|provide|send|upload|attach|include|apply|contact)\b',
            normalized,
        ):
            unconfirmed.append(clause)

    return conflicts, unconfirmed


def compute_match_score(user_profile: dict[str, Any], opportunity: dict[str, Any]) -> dict[str, Any]:
    user = user_profile or {}
    opp = opportunity or {}
    reasons = []
    strong_matches = []
    missing = []
    risks = []
    generic_requirement_conflicts, generic_requirements_unconfirmed = _generic_requirement_assessment(
        user,
        opp.get('requirements'),
    )
    if generic_requirement_conflicts:
        missing.append(
            'Explicit eligibility requirements conflict with the profile: '
            + '; '.join(generic_requirement_conflicts)
        )
        risks.append('At least one explicit generic requirement conflicts with the profile.')
    if generic_requirements_unconfirmed:
        missing.append(
            'Explicit eligibility requirements need review: '
            + '; '.join(generic_requirements_unconfirmed)
        )
        risks.append('Some explicit generic requirements cannot be confirmed from available profile fields.')
    points = {
        'skills': 0,
        'education': 0,
        'experience': 0,
        'location': 0,
        'opportunity_type': 0,
        'languages': 0,
        'work_mode': 0,
        'visa_sponsorship': 0,
        'preferences': 0,
        'qualifications': 0,
    }

    user_skills = _norm(user.get('skills'))
    required_skills = _norm(opp.get('skills'))
    if required_skills:
        matched_skills = user_skills & required_skills
        points['skills'] = round(35 * len(matched_skills) / len(required_skills))
        if matched_skills:
            strong_matches.append('Matching listed skills: ' + ', '.join(sorted(matched_skills)) + '.')
            reasons.append('Your listed skills overlap with the opportunity skills.')
        missing_skills = required_skills - user_skills
        if missing_skills:
            missing.append('Skills not listed in your profile: ' + ', '.join(sorted(missing_skills)) + '.')
    else:
        reasons.append('The opportunity does not list structured skills; no skill match was assumed.')
        risks.append('Structured opportunity skills are unavailable.')

    education_required = _text(opp.get('education_requirements'))
    user_education = ' '.join(filter(None, (
        _text(user.get('education')),
        _text(user.get('degree')),
        ' '.join(_items(user.get('certifications'))),
    )))
    if education_required:
        if user_education:
            required_terms = _terms(education_required)
            education_terms = _terms(user_education)
            overlap = required_terms & education_terms
            if overlap:
                points['education'] = round(10 * len(overlap) / max(len(required_terms), 1))
                strong_matches.append('Education terms in your profile matching the requirement: ' + ', '.join(sorted(overlap)) + '.')
                reasons.append('Education information overlaps with the stated requirements.')
            if required_terms - education_terms:
                missing.append('Education requirement terms not present in your profile: ' + ', '.join(sorted(required_terms - education_terms)) + '.')
            if not overlap:
                missing.append('Review the stated education requirement against your degree and education details.')
                risks.append('The profile does not explicitly confirm the requested education qualification.')
        else:
            missing.append('Education information is missing for the stated education requirement.')
            risks.append('Education eligibility cannot be confirmed from the profile.')

    experience_required = _text(opp.get('experience_requirements'))
    user_experience = _text(user.get('work_experience'))
    if experience_required:
        if user_experience:
            requirement_terms = _terms(experience_required)
            experience_terms = _terms(user_experience)
            overlap = requirement_terms & experience_terms
            if overlap:
                points['experience'] = round(10 * len(overlap) / max(len(requirement_terms), 1))
                strong_matches.append('Experience terms in your profile matching the requirement: ' + ', '.join(sorted(overlap)) + '.')
                reasons.append('Your experience description has terms in common with the opportunity requirements.')
            if requirement_terms - experience_terms:
                missing.append('Experience requirement terms not present in your profile: ' + ', '.join(sorted(requirement_terms - experience_terms)) + '.')
            if not overlap:
                missing.append('Compare your work-experience details with the stated experience requirements.')
                risks.append('Experience eligibility cannot be confirmed by direct profile text overlap.')
        else:
            missing.append('Work experience is missing for the stated experience requirement.')
            risks.append('Experience eligibility cannot be confirmed from the profile.')

    country = _text(opp.get('country')).casefold()
    targets = _norm(user.get('target_countries'))
    current = _text(user.get('current_country')).casefold()
    worldwide_preference = user.get('worldwide_preference') is True
    has_location_preferences = bool(targets or current or worldwide_preference)
    location_match = (
        not has_location_preferences
        or worldwide_preference
        or bool(country and (country in targets or country == current))
        or opp.get('remote_worldwide') is True
    )
    if opp.get('remote_worldwide') is True:
        points['location'] = 20
        reasons.append('The opportunity explicitly accepts worldwide remote applicants.')
        strong_matches.append('Worldwide remote eligibility is explicitly confirmed.')
    elif country and country in targets:
        points['location'] = 20
        reasons.append('The opportunity country matches one of your target countries.')
        strong_matches.append(f'Location matches your target country: {opp.get("country")}.')
    elif country and country == current:
        points['location'] = 20
        reasons.append('The opportunity country matches your current country.')
        strong_matches.append(f'Location matches your current country: {opp.get("country")}.')
    elif worldwide_preference:
        points['location'] = 18
        reasons.append('Your profile requests worldwide opportunities.')
        if not country:
            risks.append('The opportunity location is unspecified.')
    elif country and has_location_preferences:
        missing.append('The opportunity country is outside your saved country preferences.')
    elif not country:
        risks.append('The opportunity country is unspecified.')
    else:
        points['location'] = 10

    opportunity_type = _text(opp.get('opportunity_type')).casefold()
    preferred_types = _norm(user.get('preferred_opportunity_types'))
    if opportunity_type and preferred_types:
        if opportunity_type in preferred_types:
            points['opportunity_type'] = 12
            reasons.append('The opportunity type matches your preferences.')
            strong_matches.append(f'Preferred opportunity type: {opportunity_type}.')
        else:
            risks.append('The opportunity type is not in your selected preferences.')
    elif opportunity_type and not preferred_types:
        points['opportunity_type'] = 8
    elif not opportunity_type:
        risks.append('The opportunity type could not be confirmed.')

    opportunity_languages = _norm(opp.get('languages'))
    profile_languages = _norm(user.get('languages'))
    if opportunity_languages:
        shared_languages = opportunity_languages & profile_languages
        points['languages'] = round(5 * len(shared_languages) / len(opportunity_languages))
        if shared_languages:
            reasons.append('Your listed languages overlap with the opportunity language requirements.')
            strong_matches.append('Matching languages: ' + ', '.join(sorted(shared_languages)) + '.')
        if opportunity_languages - profile_languages:
            missing.append('Languages not listed in your profile: ' + ', '.join(sorted(opportunity_languages - profile_languages)) + '.')
    else:
        risks.append('No structured language requirements are available.')

    work_mode = _text(opp.get('work_mode')).casefold()
    if not work_mode and opp.get('remote_worldwide') is True:
        work_mode = 'remote'
    preferred_modes = _norm(user.get('preferred_work_modes'))
    mode_match = not preferred_modes or not work_mode or work_mode in preferred_modes
    if work_mode and preferred_modes:
        if mode_match:
            points['work_mode'] = 5
            reasons.append('Work mode matches your preference.')
            strong_matches.append(f'Preferred work mode: {work_mode}.')
        else:
            missing.append('Work mode is outside your saved preferences.')
            risks.append('The work mode conflicts with your preferences.')
    elif work_mode:
        points['work_mode'] = 4
    else:
        risks.append('The work mode is unspecified.')

    sponsorship = opp.get('visa_sponsorship')
    if sponsorship is True:
        if user.get('visa_sponsorship_preference') is True:
            points['visa_sponsorship'] = 8
            reasons.append('Visa sponsorship is available and matches your preference.')
            strong_matches.append('Visa sponsorship is explicitly available.')
        else:
            points['visa_sponsorship'] = 4
    elif sponsorship is False and user.get('visa_sponsorship_preference') is True:
        missing.append('Visa sponsorship is preferred but is explicitly unavailable.')
        risks.append('Visa sponsorship preference is not met.')
    elif sponsorship is None:
        risks.append('Visa sponsorship availability is unspecified.')

    salary_preference = _text(user.get('salary_stipend_preference'))
    salary_offered = _text(opp.get('salary_stipend'))
    if salary_preference and salary_offered:
        if re.sub(r'\s+', ' ', salary_preference).casefold() == re.sub(r'\s+', ' ', salary_offered).casefold():
            points['preferences'] = 5
            reasons.append('Advertised compensation text exactly matches your saved preference.')
            strong_matches.append('Advertised compensation exactly matches your saved preference.')
        else:
            risks.append('Advertised compensation could not be matched confidently to your preference.')
    elif salary_preference and not salary_offered:
        risks.append('The opportunity does not specify compensation for comparison with your preference.')

    qualifications = _text(opp.get('qualifications'))
    if qualifications:
        profile_qualifications = ' '.join(filter(None, (
            _text(user.get('degree')),
            ' '.join(_items(user.get('certifications'))),
            _text(user.get('education')),
        )))
        if profile_qualifications:
            qualification_terms = _terms(qualifications)
            profile_terms = _terms(profile_qualifications)
            shared_terms = qualification_terms & profile_terms
            if shared_terms:
                points['qualifications'] = round(10 * len(shared_terms) / max(len(qualification_terms), 1))
                strong_matches.append('Qualification terms in your profile matching the listing: ' + ', '.join(sorted(shared_terms)) + '.')
                reasons.append('Your profile includes terms that overlap with the stated qualifications.')
            if qualification_terms - profile_terms:
                missing.append('Qualification terms not present in your profile: ' + ', '.join(sorted(qualification_terms - profile_terms)) + '.')
            if not shared_terms:
                missing.append('Compare your degree and certifications with the listed qualifications.')
                risks.append('The profile does not explicitly confirm the listed qualifications.')
        else:
            missing.append('Degree or certification details are missing for the listed qualifications.')
            risks.append('Qualification eligibility cannot be confirmed from the profile.')
    else:
        risks.append('No structured qualification requirements are available.')

    core_score = sum(points[key] for key in (
        'skills',
        'location',
        'opportunity_type',
        'visa_sponsorship',
    ))
    additional_score = sum(points[key] for key in (
        'education',
        'experience',
        'languages',
        'work_mode',
        'preferences',
        'qualifications',
    ))
    score = max(0, min(100, round(core_score + additional_score * 25 / 45)))
    threshold = _threshold(user.get('minimum_ai_match_score', 75))
    threshold_met = score >= threshold
    generic_requirements_clear = not generic_requirement_conflicts and not generic_requirements_unconfirmed
    eligible = threshold_met and location_match and mode_match and generic_requirements_clear
    if not location_match:
        eligibility_status = 'location_mismatch'
    elif not mode_match:
        eligibility_status = 'work_mode_mismatch'
    elif generic_requirement_conflicts:
        eligibility_status = 'requirements_conflict'
    elif generic_requirements_unconfirmed:
        eligibility_status = 'requirements_unconfirmed'
    elif not threshold_met:
        eligibility_status = 'below_threshold'
    else:
        eligibility_status = 'eligible'
    if not location_match or not mode_match:
        risks.append('A location or work-mode preference prevents eligibility despite the score.')
    if eligible:
        recommended_action = 'queue' if user.get('auto_apply') else 'review_and_apply'
    else:
        recommended_action = 'review' if score >= max(0, threshold - 15) else 'skip'
    if generic_requirement_conflicts or generic_requirements_unconfirmed:
        recommended_action = 'review'
    if missing and eligible:
        recommended_action = 'review_and_apply'

    unique_reasons = list(dict.fromkeys(reasons))
    unique_missing = list(dict.fromkeys(missing))
    unique_risks = list(dict.fromkeys(risks))
    return {
        'score': score,
        'match_score': score,
        'reasons': unique_reasons,
        'why_it_matches': unique_reasons,
        'missing': unique_missing,
        'missing_requirements': unique_missing,
        'strong_matches': list(dict.fromkeys(strong_matches)),
        'risks': unique_risks,
        'risk_factors': unique_risks,
        'eligible': eligible,
        'eligibility_status': eligibility_status,
        'threshold_met': threshold_met,
        'location_match': location_match,
        'work_mode_match': mode_match,
        'manual_override_allowed': (
            not threshold_met
            and location_match
            and mode_match
            and generic_requirements_clear
        ),
        'recommended_action': recommended_action,
        'requirements_status': (
            'conflict' if generic_requirement_conflicts else
            'unconfirmed' if generic_requirements_unconfirmed else 'clear'
        ),
        'minimum_score': threshold,
        'factor_scores': points,
    }
