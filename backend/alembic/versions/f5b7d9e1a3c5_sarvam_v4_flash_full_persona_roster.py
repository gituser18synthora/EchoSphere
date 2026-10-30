"""Sarvam bulbul:v4-flash: the rest of Sarvam's persona roster as catalog voices.

Revision ID: f5b7d9e1a3c5
Revises: e4b6d8f0a2c4
Create Date: 2026-10-26

c2e4a6b8d0f1 put 36 of Sarvam's bulbul:v4-flash personas in the voice
catalog — the Hindi/English ones of the reviewed v3 -> v4 mapping. Sarvam's
roster has 222 personas for the languages the model accepts (snapshot of the
live API's speaker list and the docs' voice catalog, 2026-10-26), so the
Studio, Platform Configuration and AI profiles could not offer the other 186.

Inserts each missing one as a voice profile ``vp-sv4-<persona>`` for
bulbul:v4-flash only, offered for the language(s) of its persona tag (``enhi``
= Hindi and English). Insert-only: an existing row — including any an
operator edited — is never touched, and no bot, model or default changes.

Gender: a speaker that also has a bulbul:v3 voice keeps that voice's catalog
gender; a new speaker takes the gender Sarvam publishes, else "neutral"
(unpublished; operators set it in Platform Configuration -> Voices).

Rows: snapshot of backend.seeds.provider_catalog_seed.sarvam_v4_roster_voices()
(2026-10-26). Rollback deletes exactly these ids; a bot that selected one of
them fails voice validation afterwards, so move it first.
"""
import json
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5b7d9e1a3c5"
down_revision: Union[str, None] = "e4b6d8f0a2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL_CODE = "bulbul:v4-flash"
_SAMPLE_TEXT = "Hello! I can help you book your next appointment in just a minute."
#: After the 37 bulbul:v3 speakers and the 36 personas of c2e4a6b8d0f1.
_FIRST_SORT = 73

# (id, name, gender, languages, style, wire persona)
_ROSTER_VOICES = (
    ("vp-sv4-aditya_hi_sales", "Aditya · Hindi · Sales", "male", ('hi-IN',), "Sales", "aditya_hi_sales"),
    ("vp-sv4-anand_hi_documentary", "Anand · Hindi · Documentary", "male", ('hi-IN',), "Documentary", "anand_hi_documentary"),
    ("vp-sv4-aparna_hi_customer", "Aparna · Hindi · Customer", "female", ('hi-IN',), "Customer", "aparna_hi_customer"),
    ("vp-sv4-aparna_hi_kyc", "Aparna · Hindi · Kyc", "female", ('hi-IN',), "Kyc", "aparna_hi_kyc"),
    ("vp-sv4-ashok_hi_character", "Ashok · Hindi · Character", "male", ('hi-IN',), "Character", "ashok_hi_character"),
    ("vp-sv4-ashok_hi_news", "Ashok · Hindi · News", "male", ('hi-IN',), "News", "ashok_hi_news"),
    ("vp-sv4-chhavi_hi_kids", "Chhavi · Hindi · Kids", "neutral", ('hi-IN',), "Kids", "chhavi_hi_kids"),
    ("vp-sv4-ishita_hi_ads", "Ishita · Hindi · Ads", "female", ('hi-IN',), "Ads", "ishita_hi_ads"),
    ("vp-sv4-ishita_hi_edtech", "Ishita · Hindi · Edtech", "female", ('hi-IN',), "Edtech", "ishita_hi_edtech"),
    ("vp-sv4-ishita_hi_banking", "Ishita · Hindi · Banking", "female", ('hi-IN',), "Banking", "ishita_hi_banking"),
    ("vp-sv4-ishita_hi_ads_informal", "Ishita · Hindi · Ads informal", "female", ('hi-IN',), "Ads informal", "ishita_hi_ads_informal"),
    ("vp-sv4-ishita_hi_devotional", "Ishita · Hindi · Devotional", "female", ('hi-IN',), "Devotional", "ishita_hi_devotional"),
    ("vp-sv4-ishita_hi_numbers", "Ishita · Hindi · Numbers", "female", ('hi-IN',), "Numbers", "ishita_hi_numbers"),
    ("vp-sv4-ishita_hi_social", "Ishita · Hindi · Social", "female", ('hi-IN',), "Social", "ishita_hi_social"),
    ("vp-sv4-kunal_hi_kids", "Kunal · Hindi · Kids", "neutral", ('hi-IN',), "Kids", "kunal_hi_kids"),
    ("vp-sv4-mahesh_hi_documentary", "Mahesh · Hindi · Documentary", "neutral", ('hi-IN',), "Documentary", "mahesh_hi_documentary"),
    ("vp-sv4-mani_hi_devotional", "Mani · Hindi · Devotional", "male", ('hi-IN',), "Devotional", "mani_hi_devotional"),
    ("vp-sv4-nachiket_hi_devotional", "Nachiket · Hindi · Devotional", "neutral", ('hi-IN',), "Devotional", "nachiket_hi_devotional"),
    ("vp-sv4-ratan_hi_latenight", "Ratan · Hindi · Latenight", "male", ('hi-IN',), "Latenight", "ratan_hi_latenight"),
    ("vp-sv4-ratan_hi_documentary", "Ratan · Hindi · Documentary", "male", ('hi-IN',), "Documentary", "ratan_hi_documentary"),
    ("vp-sv4-ratan_hi_devotional", "Ratan · Hindi · Devotional", "male", ('hi-IN',), "Devotional", "ratan_hi_devotional"),
    ("vp-sv4-ratan_hi_recovery", "Ratan · Hindi · Recovery", "male", ('hi-IN',), "Recovery", "ratan_hi_recovery"),
    ("vp-sv4-ratan_hi_social", "Ratan · Hindi · Social", "male", ('hi-IN',), "Social", "ratan_hi_social"),
    ("vp-sv4-ratan_hi_sports", "Ratan · Hindi · Sports", "male", ('hi-IN',), "Sports", "ratan_hi_sports"),
    ("vp-sv4-ratan_hi_latenight_warm", "Ratan · Hindi · Latenight warm", "male", ('hi-IN',), "Latenight warm", "ratan_hi_latenight_warm"),
    ("vp-sv4-ritu_hi_customer_utility", "Ritu · Hindi · Customer utility", "female", ('hi-IN',), "Customer utility", "ritu_hi_customer_utility"),
    ("vp-sv4-ritu_hi_kids", "Ritu · Hindi · Kids", "female", ('hi-IN',), "Kids", "ritu_hi_kids"),
    ("vp-sv4-ritu_hi_conversation", "Ritu · Hindi · Conversation", "female", ('hi-IN',), "Conversation", "ritu_hi_conversation"),
    ("vp-sv4-ritu_hi_edtech", "Ritu · Hindi · Edtech", "female", ('hi-IN',), "Edtech", "ritu_hi_edtech"),
    ("vp-sv4-ritu_hi_ads_formal", "Ritu · Hindi · Ads formal", "female", ('hi-IN',), "Ads formal", "ritu_hi_ads_formal"),
    ("vp-sv4-ritu_hi_banking", "Ritu · Hindi · Banking", "female", ('hi-IN',), "Banking", "ritu_hi_banking"),
    ("vp-sv4-ritu_hi_ads_informal", "Ritu · Hindi · Ads informal", "female", ('hi-IN',), "Ads informal", "ritu_hi_ads_informal"),
    ("vp-sv4-ritu_hi_insurance", "Ritu · Hindi · Insurance", "female", ('hi-IN',), "Insurance", "ritu_hi_insurance"),
    ("vp-sv4-ritu_hi_edtech_bot", "Ritu · Hindi · Edtech bot", "female", ('hi-IN',), "Edtech bot", "ritu_hi_edtech_bot"),
    ("vp-sv4-ritu_hi_medical", "Ritu · Hindi · Medical", "female", ('hi-IN',), "Medical", "ritu_hi_medical"),
    ("vp-sv4-ritu_hi_sales", "Ritu · Hindi · Sales", "female", ('hi-IN',), "Sales", "ritu_hi_sales"),
    ("vp-sv4-ritu_hi_reels", "Ritu · Hindi · Reels", "female", ('hi-IN',), "Reels", "ritu_hi_reels"),
    ("vp-sv4-ritu_hi_social", "Ritu · Hindi · Social", "female", ('hi-IN',), "Social", "ritu_hi_social"),
    ("vp-sv4-ritu_hi_customer_warm", "Ritu · Hindi · Customer warm", "female", ('hi-IN',), "Customer warm", "ritu_hi_customer_warm"),
    ("vp-sv4-ritu_hi_social_lively", "Ritu · Hindi · Social lively", "female", ('hi-IN',), "Social lively", "ritu_hi_social_lively"),
    ("vp-sv4-roopa_hi_companion", "Roopa · Hindi · Companion", "female", ('hi-IN',), "Companion", "roopa_hi_companion"),
    ("vp-sv4-roopa_hi_narration", "Roopa · Hindi · Narration", "female", ('hi-IN',), "Narration", "roopa_hi_narration"),
    ("vp-sv4-roopa_hi_recovery", "Roopa · Hindi · Recovery", "female", ('hi-IN',), "Recovery", "roopa_hi_recovery"),
    ("vp-sv4-roopa_hi_market", "Roopa · Hindi · Market", "female", ('hi-IN',), "Market", "roopa_hi_market"),
    ("vp-sv4-sanchita_hi_assistant", "Sanchita · Hindi · Assistant", "female", ('hi-IN',), "Assistant", "sanchita_hi_assistant"),
    ("vp-sv4-sanchita_hi_edtech", "Sanchita · Hindi · Edtech", "female", ('hi-IN',), "Edtech", "sanchita_hi_edtech"),
    ("vp-sv4-sanchita_hi_banking", "Sanchita · Hindi · Banking", "female", ('hi-IN',), "Banking", "sanchita_hi_banking"),
    ("vp-sv4-sanchita_hi_feedback", "Sanchita · Hindi · Feedback", "female", ('hi-IN',), "Feedback", "sanchita_hi_feedback"),
    ("vp-sv4-sanchita_hi_ads_formal", "Sanchita · Hindi · Ads formal", "female", ('hi-IN',), "Ads formal", "sanchita_hi_ads_formal"),
    ("vp-sv4-sanchita_hi_ads_informal", "Sanchita · Hindi · Ads informal", "female", ('hi-IN',), "Ads informal", "sanchita_hi_ads_informal"),
    ("vp-sv4-sanchita_hi_interview", "Sanchita · Hindi · Interview", "female", ('hi-IN',), "Interview", "sanchita_hi_interview"),
    ("vp-sv4-sanchita_hi_romantic", "Sanchita · Hindi · Romantic", "female", ('hi-IN',), "Romantic", "sanchita_hi_romantic"),
    ("vp-sv4-sanchita_hi_market", "Sanchita · Hindi · Market", "female", ('hi-IN',), "Market", "sanchita_hi_market"),
    ("vp-sv4-sanchita_hi_social", "Sanchita · Hindi · Social", "female", ('hi-IN',), "Social", "sanchita_hi_social"),
    ("vp-sv4-sanchita_hi_kyc", "Sanchita · Hindi · Kyc", "female", ('hi-IN',), "Kyc", "sanchita_hi_kyc"),
    ("vp-sv4-sarika_hi_conversation", "Sarika · Hindi · Conversation", "neutral", ('hi-IN',), "Conversation", "sarika_hi_conversation"),
    ("vp-sv4-shalini_hi_companion", "Shalini · Hindi · Companion", "female", ('hi-IN',), "Companion", "shalini_hi_companion"),
    ("vp-sv4-shalini_hi_social", "Shalini · Hindi · Social", "female", ('hi-IN',), "Social", "shalini_hi_social"),
    ("vp-sv4-shreya_hi_news", "Shreya · Hindi · News", "female", ('hi-IN',), "News", "shreya_hi_news"),
    ("vp-sv4-shubh_hi_ecomm", "Shubh · Hindi · Ecomm", "male", ('hi-IN',), "Ecomm", "shubh_hi_ecomm"),
    ("vp-sv4-shubh_hi_stories_mixed", "Shubh · Hindi · Stories mixed", "male", ('hi-IN',), "Stories mixed", "shubh_hi_stories_mixed"),
    ("vp-sv4-shubh_hi_devotional", "Shubh · Hindi · Devotional", "male", ('hi-IN',), "Devotional", "shubh_hi_devotional"),
    ("vp-sv4-shubh_hi_ads", "Shubh · Hindi · Ads", "male", ('hi-IN',), "Ads", "shubh_hi_ads"),
    ("vp-sv4-shubh_hi_recovery", "Shubh · Hindi · Recovery", "male", ('hi-IN',), "Recovery", "shubh_hi_recovery"),
    ("vp-sv4-shubh_hi_stories_dramatic", "Shubh · Hindi · Stories dramatic", "male", ('hi-IN',), "Stories dramatic", "shubh_hi_stories_dramatic"),
    ("vp-sv4-simran_hi_assistant", "Simran · Hindi · Assistant", "female", ('hi-IN',), "Assistant", "simran_hi_assistant"),
    ("vp-sv4-simran_hi_narration", "Simran · Hindi · Narration", "female", ('hi-IN',), "Narration", "simran_hi_narration"),
    ("vp-sv4-simran_hi_automobile", "Simran · Hindi · Automobile", "female", ('hi-IN',), "Automobile", "simran_hi_automobile"),
    ("vp-sv4-simran_hi_conversation", "Simran · Hindi · Conversation", "female", ('hi-IN',), "Conversation", "simran_hi_conversation"),
    ("vp-sv4-simran_hi_news_breaking", "Simran · Hindi · News breaking", "female", ('hi-IN',), "News breaking", "simran_hi_news_breaking"),
    ("vp-sv4-simran_hi_social_energetic", "Simran · Hindi · Social energetic", "female", ('hi-IN',), "Social energetic", "simran_hi_social_energetic"),
    ("vp-sv4-simran_hi_social_excited", "Simran · Hindi · Social excited", "female", ('hi-IN',), "Social excited", "simran_hi_social_excited"),
    ("vp-sv4-simran_hi_latenight", "Simran · Hindi · Latenight", "female", ('hi-IN',), "Latenight", "simran_hi_latenight"),
    ("vp-sv4-simran_hi_news", "Simran · Hindi · News", "female", ('hi-IN',), "News", "simran_hi_news"),
    ("vp-sv4-simran_hi_recovery", "Simran · Hindi · Recovery", "female", ('hi-IN',), "Recovery", "simran_hi_recovery"),
    ("vp-sv4-simran_hi_sales", "Simran · Hindi · Sales", "female", ('hi-IN',), "Sales", "simran_hi_sales"),
    ("vp-sv4-suchitra_hi_ecomm", "Suchitra · Hindi · Ecomm", "neutral", ('hi-IN',), "Ecomm", "suchitra_hi_ecomm"),
    ("vp-sv4-sunny_hi_ads", "Sunny · Hindi · Ads", "male", ('hi-IN',), "Ads", "sunny_hi_ads"),
    ("vp-sv4-sunny_hi_reels", "Sunny · Hindi · Reels", "male", ('hi-IN',), "Reels", "sunny_hi_reels"),
    ("vp-sv4-tarun_hi_sales", "Tarun · Hindi · Sales", "male", ('hi-IN',), "Sales", "tarun_hi_sales"),
    ("vp-sv4-chaitra_hi_customer", "Chaitra · Hindi · Customer", "neutral", ('hi-IN',), "Customer", "chaitra_hi_customer"),
    ("vp-sv4-shilpa_hi_narration", "Shilpa · Hindi · Narration", "neutral", ('hi-IN',), "Narration", "shilpa_hi_narration"),
    ("vp-sv4-aarti_hi_customer", "Aarti · Hindi · Customer", "neutral", ('hi-IN',), "Customer", "aarti_hi_customer"),
    ("vp-sv4-aryaman_hi_ads", "Aryaman · Hindi · Ads", "neutral", ('hi-IN',), "Ads", "aryaman_hi_ads"),
    ("vp-sv4-chirag_hi_social", "Chirag · Hindi · Social", "neutral", ('hi-IN',), "Social", "chirag_hi_social"),
    ("vp-sv4-girish_hi_devotional", "Girish · Hindi · Devotional", "neutral", ('hi-IN',), "Devotional", "girish_hi_devotional"),
    ("vp-sv4-mukul_hi_ads", "Mukul · Hindi · Ads", "neutral", ('hi-IN',), "Ads", "mukul_hi_ads"),
    ("vp-sv4-mukul_hi_suspense", "Mukul · Hindi · Suspense", "neutral", ('hi-IN',), "Suspense", "mukul_hi_suspense"),
    ("vp-sv4-suman_hi_companion", "Suman · Hindi · Companion", "neutral", ('hi-IN',), "Companion", "suman_hi_companion"),
    ("vp-sv4-vaibhav_hi_social", "Vaibhav · Hindi · Social", "neutral", ('hi-IN',), "Social", "vaibhav_hi_social"),
    ("vp-sv4-vandana_hi_ecomm", "Vandana · Hindi · Ecomm", "neutral", ('hi-IN',), "Ecomm", "vandana_hi_ecomm"),
    ("vp-sv4-vipul_hi_social", "Vipul · Hindi · Social", "neutral", ('hi-IN',), "Social", "vipul_hi_social"),
    ("vp-sv4-arnab_bn_conversation", "Arnab · Bengali · Conversation", "neutral", ('bn-IN',), "Conversation", "arnab_bn_conversation"),
    ("vp-sv4-roopa_bn_conversational", "Roopa · Bengali · Conversational", "female", ('bn-IN',), "Conversational", "roopa_bn_conversational"),
    ("vp-sv4-bimal_bn_suspense", "Bimal · Bengali · Suspense", "neutral", ('bn-IN',), "Suspense", "bimal_bn_suspense"),
    ("vp-sv4-aditi_en_stories", "Aditi · English · Stories", "neutral", ('en-IN',), "Stories", "aditi_en_stories"),
    ("vp-sv4-aparna_en_companion", "Aparna · English · Companion", "female", ('en-IN',), "Companion", "aparna_en_companion"),
    ("vp-sv4-aparna_en_edtech", "Aparna · English · Edtech", "female", ('en-IN',), "Edtech", "aparna_en_edtech"),
    ("vp-sv4-ashwin_en_sports", "Ashwin · English · Sports", "neutral", ('en-IN',), "Sports", "ashwin_en_sports"),
    ("vp-sv4-ashwin_en_sports_energetic", "Ashwin · English · Sports energetic", "neutral", ('en-IN',), "Sports energetic", "ashwin_en_sports_energetic"),
    ("vp-sv4-chandrika_en_stories", "Chandrika · English · Stories", "neutral", ('en-IN',), "Stories", "chandrika_en_stories"),
    ("vp-sv4-dev_en_recovery", "Dev · English · Recovery", "male", ('en-IN',), "Recovery", "dev_en_recovery"),
    ("vp-sv4-deven_en_conversation", "Deven · English · Conversation", "neutral", ('en-IN',), "Conversation", "deven_en_conversation"),
    ("vp-sv4-ishita_en_medical", "Ishita · English · Medical", "female", ('en-IN',), "Medical", "ishita_en_medical"),
    ("vp-sv4-ishita_en_numbers", "Ishita · English · Numbers", "female", ('en-IN',), "Numbers", "ishita_en_numbers"),
    ("vp-sv4-ishita_en_social", "Ishita · English · Social", "female", ('en-IN',), "Social", "ishita_en_social"),
    ("vp-sv4-ishita_en_stories", "Ishita · English · Stories", "female", ('en-IN',), "Stories", "ishita_en_stories"),
    ("vp-sv4-kalpit_en_edtech", "Kalpit · English · Edtech", "neutral", ('en-IN',), "Edtech", "kalpit_en_edtech"),
    ("vp-sv4-nachiket_en_ads", "Nachiket · English · Ads", "neutral", ('en-IN',), "Ads", "nachiket_en_ads"),
    ("vp-sv4-neha_en_latenight", "Neha · English · Latenight", "female", ('en-IN',), "Latenight", "neha_en_latenight"),
    ("vp-sv4-nupur_en_kids", "Nupur · English · Kids", "neutral", ('en-IN',), "Kids", "nupur_en_kids"),
    ("vp-sv4-ojas_en_social", "Ojas · English · Social", "neutral", ('en-IN',), "Social", "ojas_en_social"),
    ("vp-sv4-ritu_en_latenight", "Ritu · English · Latenight", "female", ('en-IN',), "Latenight", "ritu_en_latenight"),
    ("vp-sv4-ritu_en_medical", "Ritu · English · Medical", "female", ('en-IN',), "Medical", "ritu_en_medical"),
    ("vp-sv4-ritu_en_reels", "Ritu · English · Reels", "female", ('en-IN',), "Reels", "ritu_en_reels"),
    ("vp-sv4-rustom_en_suspense", "Rustom · English · Suspense", "neutral", ('en-IN',), "Suspense", "rustom_en_suspense"),
    ("vp-sv4-sanchita_en_companion", "Sanchita · English · Companion", "female", ('en-IN',), "Companion", "sanchita_en_companion"),
    ("vp-sv4-sanchita_en_insurance", "Sanchita · English · Insurance", "female", ('en-IN',), "Insurance", "sanchita_en_insurance"),
    ("vp-sv4-sanchita_en_recovery", "Sanchita · English · Recovery", "female", ('en-IN',), "Recovery", "sanchita_en_recovery"),
    ("vp-sv4-sanchita_en_market", "Sanchita · English · Market", "female", ('en-IN',), "Market", "sanchita_en_market"),
    ("vp-sv4-sanchita_en_social", "Sanchita · English · Social", "female", ('en-IN',), "Social", "sanchita_en_social"),
    ("vp-sv4-shabana_en_edtech", "Shabana · English · Edtech", "neutral", ('en-IN',), "Edtech", "shabana_en_edtech"),
    ("vp-sv4-shalini_en_companion", "Shalini · English · Companion", "female", ('en-IN',), "Companion", "shalini_en_companion"),
    ("vp-sv4-shalini_en_customer", "Shalini · English · Customer", "female", ('en-IN',), "Customer", "shalini_en_customer"),
    ("vp-sv4-shubh_en_narration", "Shubh · English · Narration", "male", ('en-IN',), "Narration", "shubh_en_narration"),
    ("vp-sv4-shubh_en_numbers", "Shubh · English · Numbers", "male", ('en-IN',), "Numbers", "shubh_en_numbers"),
    ("vp-sv4-shubh_en_ads", "Shubh · English · Ads", "male", ('en-IN',), "Ads", "shubh_en_ads"),
    ("vp-sv4-shubh_en_recovery", "Shubh · English · Recovery", "male", ('en-IN',), "Recovery", "shubh_en_recovery"),
    ("vp-sv4-shubh_en_audiobook", "Shubh · English · Audiobook", "male", ('en-IN',), "Audiobook", "shubh_en_audiobook"),
    ("vp-sv4-shubh_en_narration_gentle", "Shubh · English · Narration gentle", "male", ('en-IN',), "Narration gentle", "shubh_en_narration_gentle"),
    ("vp-sv4-shubh_en_sports", "Shubh · English · Sports", "male", ('en-IN',), "Sports", "shubh_en_sports"),
    ("vp-sv4-simran_en_narration", "Simran · English · Narration", "female", ('en-IN',), "Narration", "simran_en_narration"),
    ("vp-sv4-simran_en_automobile", "Simran · English · Automobile", "female", ('en-IN',), "Automobile", "simran_en_automobile"),
    ("vp-sv4-simran_en_conversation", "Simran · English · Conversation", "female", ('en-IN',), "Conversation", "simran_en_conversation"),
    ("vp-sv4-simran_en_edtech", "Simran · English · Edtech", "female", ('en-IN',), "Edtech", "simran_en_edtech"),
    ("vp-sv4-simran_en_edtech_bot", "Simran · English · Edtech bot", "female", ('en-IN',), "Edtech bot", "simran_en_edtech_bot"),
    ("vp-sv4-simran_en_sales", "Simran · English · Sales", "female", ('en-IN',), "Sales", "simran_en_sales"),
    ("vp-sv4-simran_en_recovery", "Simran · English · Recovery", "female", ('en-IN',), "Recovery", "simran_en_recovery"),
    ("vp-sv4-simran_en_ads", "Simran · English · Ads", "female", ('en-IN',), "Ads", "simran_en_ads"),
    ("vp-sv4-simran_en_therapist", "Simran · English · Therapist", "female", ('en-IN',), "Therapist", "simran_en_therapist"),
    ("vp-sv4-sunny_en_social", "Sunny · English · Social", "male", ('en-IN',), "Social", "sunny_en_social"),
    ("vp-sv4-varun_en_suspense", "Varun · English · Suspense", "male", ('en-IN',), "Suspense", "varun_en_suspense"),
    ("vp-sv4-zarina_en_conversation", "Zarina · English · Conversation", "neutral", ('en-IN',), "Conversation", "zarina_en_conversation"),
    ("vp-sv4-amelia_en_conversational", "Amelia · English · Conversational", "neutral", ('en-IN',), "Conversational", "amelia_en_conversational"),
    ("vp-sv4-sophia_en_conversational", "Sophia · English · Conversational", "neutral", ('en-IN',), "Conversational", "sophia_en_conversational"),
    ("vp-sv4-girish_en_documentary", "Girish · English · Documentary", "neutral", ('en-IN',), "Documentary", "girish_en_documentary"),
    ("vp-sv4-girish_en_devotional", "Girish · English · Devotional", "neutral", ('en-IN',), "Devotional", "girish_en_devotional"),
    ("vp-sv4-payal_en_edtech", "Payal · English · Edtech", "neutral", ('en-IN',), "Edtech", "payal_en_edtech"),
    ("vp-sv4-sarang_en_narration", "Sarang · English · Narration", "neutral", ('en-IN',), "Narration", "sarang_en_narration"),
    ("vp-sv4-ishita_enhi_companion", "Ishita · Hinglish · Companion", "female", ('hi-IN', 'en-IN'), "Companion", "ishita_enhi_companion"),
    ("vp-sv4-ishita_enhi_customer_expressive", "Ishita · Hinglish · Customer expressive", "female", ('hi-IN', 'en-IN'), "Customer expressive", "ishita_enhi_customer_expressive"),
    ("vp-sv4-sanchita_enhi_companion", "Sanchita · Hinglish · Companion", "female", ('hi-IN', 'en-IN'), "Companion", "sanchita_enhi_companion"),
    ("vp-sv4-shalini_enhi_companion", "Shalini · Hinglish · Companion", "female", ('hi-IN', 'en-IN'), "Companion", "shalini_enhi_companion"),
    ("vp-sv4-shalini_enhi_customer", "Shalini · Hinglish · Customer", "female", ('hi-IN', 'en-IN'), "Customer", "shalini_enhi_customer"),
    ("vp-sv4-shubh_enhi_ads", "Shubh · Hinglish · Ads", "male", ('hi-IN', 'en-IN'), "Ads", "shubh_enhi_ads"),
    ("vp-sv4-shubh_enhi_banking", "Shubh · Hinglish · Banking", "male", ('hi-IN', 'en-IN'), "Banking", "shubh_enhi_banking"),
    ("vp-sv4-simran_enhi_companion", "Simran · Hinglish · Companion", "female", ('hi-IN', 'en-IN'), "Companion", "simran_enhi_companion"),
    ("vp-sv4-simran_enhi_banking_expressive", "Simran · Hinglish · Banking expressive", "female", ('hi-IN', 'en-IN'), "Banking expressive", "simran_enhi_banking_expressive"),
    ("vp-sv4-bhavik_gu_conversation", "Bhavik · Gujarati · Conversation", "neutral", ('gu-IN',), "Conversation", "bhavik_gu_conversation"),
    ("vp-sv4-pooja_gu_conversational", "Pooja · Gujarati · Conversational", "female", ('gu-IN',), "Conversational", "pooja_gu_conversational"),
    ("vp-sv4-pooja_gu_customer", "Pooja · Gujarati · Customer", "female", ('gu-IN',), "Customer", "pooja_gu_customer"),
    ("vp-sv4-chaitra_kn_conversation", "Chaitra · Kannada · Conversation", "neutral", ('kn-IN',), "Conversation", "chaitra_kn_conversation"),
    ("vp-sv4-chaitra_kn_narration", "Chaitra · Kannada · Narration", "neutral", ('kn-IN',), "Narration", "chaitra_kn_narration"),
    ("vp-sv4-chetan_kn_conversation", "Chetan · Kannada · Conversation", "neutral", ('kn-IN',), "Conversation", "chetan_kn_conversation"),
    ("vp-sv4-suchitra_kn_narration", "Suchitra · Kannada · Narration", "neutral", ('kn-IN',), "Narration", "suchitra_kn_narration"),
    ("vp-sv4-ishita_mr_conversational", "Ishita · Marathi · Conversational", "female", ('mr-IN',), "Conversational", "ishita_mr_conversational"),
    ("vp-sv4-mrunal_mr_narration", "Mrunal · Marathi · Narration", "neutral", ('mr-IN',), "Narration", "mrunal_mr_narration"),
    ("vp-sv4-neha_mr_narration", "Neha · Marathi · Narration", "female", ('mr-IN',), "Narration", "neha_mr_narration"),
    ("vp-sv4-nilesh_mr_conversation", "Nilesh · Marathi · Conversation", "neutral", ('mr-IN',), "Conversation", "nilesh_mr_conversation"),
    ("vp-sv4-ritu_mr_insurance", "Ritu · Marathi · Insurance", "female", ('mr-IN',), "Insurance", "ritu_mr_insurance"),
    ("vp-sv4-ritu_mr_narration", "Ritu · Marathi · Narration", "female", ('mr-IN',), "Narration", "ritu_mr_narration"),
    ("vp-sv4-rupali_mr_stories", "Rupali · Marathi · Stories", "female", ('mr-IN',), "Stories", "rupali_mr_stories"),
    ("vp-sv4-soham_mr_narration", "Soham · Marathi · Narration", "male", ('mr-IN',), "Narration", "soham_mr_narration"),
    ("vp-sv4-mukul_mr_stories", "Mukul · Marathi · Stories", "neutral", ('mr-IN',), "Stories", "mukul_mr_stories"),
    ("vp-sv4-anand_pa_conversation", "Anand · Punjabi · Conversation", "male", ('pa-IN',), "Conversation", "anand_pa_conversation"),
    ("vp-sv4-anand_pa_customer", "Anand · Punjabi · Customer", "male", ('pa-IN',), "Customer", "anand_pa_customer"),
    ("vp-sv4-harpreet_pa_narration", "Harpreet · Punjabi · Narration", "neutral", ('pa-IN',), "Narration", "harpreet_pa_narration"),
    ("vp-sv4-jaspal_pa_banking", "Jaspal · Punjabi · Banking", "neutral", ('pa-IN',), "Banking", "jaspal_pa_banking"),
    ("vp-sv4-gokul_ta_narration", "Gokul · Tamil · Narration", "male", ('ta-IN',), "Narration", "gokul_ta_narration"),
    ("vp-sv4-aravind_ta_ads", "Aravind · Tamil · Ads", "neutral", ('ta-IN',), "Ads", "aravind_ta_ads"),
    ("vp-sv4-aravind_ta_suspense", "Aravind · Tamil · Suspense", "neutral", ('ta-IN',), "Suspense", "aravind_ta_suspense"),
    ("vp-sv4-vijay_ta_narration", "Vijay · Tamil · Narration", "male", ('ta-IN',), "Narration", "vijay_ta_narration"),
    ("vp-sv4-kavitha_te_conversation", "Kavitha · Telugu · Conversation", "female", ('te-IN',), "Conversation", "kavitha_te_conversation"),
    ("vp-sv4-kavitha_te_narration", "Kavitha · Telugu · Narration", "female", ('te-IN',), "Narration", "kavitha_te_narration"),
    ("vp-sv4-pooja_te_conversation", "Pooja · Telugu · Conversation", "female", ('te-IN',), "Conversation", "pooja_te_conversation"),
    ("vp-sv4-tarun_te_narration", "Tarun · Telugu · Narration", "male", ('te-IN',), "Narration", "tarun_te_narration"),
)


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    existing = {
        row[0] for row in bind.execute(
            sa.text("SELECT id FROM voice_profiles WHERE id LIKE 'vp-sv4-%'")
        ).fetchall()
    }
    for sort, (vid, name, gender, languages, style, persona) in enumerate(
            _ROSTER_VOICES, start=_FIRST_SORT):
        if vid in existing:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO voice_profiles (id, source, name, gender, languages, "
                "locale, accent, styles, latency_ms, premium, sample_text, provider, "
                "provider_voice_id, speaking_rate, pitch, model_codes, "
                "provider_settings, is_default, status, sort_order, created_at, "
                "updated_at, is_deleted) VALUES (:id, 'platform', :name, :gender, "
                ":languages, :locale, 'Indian', :styles, 150, 0, :sample_text, "
                "'sarvam', :persona, 1.0, 1.0, :model_codes, :provider_settings, 0, "
                "'active', :sort, :now, :now, 0)"
            ),
            {
                "id": vid, "name": name, "gender": gender,
                "languages": json.dumps(list(languages)), "locale": languages[0],
                "styles": json.dumps([style]), "sample_text": _SAMPLE_TEXT,
                "persona": persona, "model_codes": json.dumps([_MODEL_CODE]),
                "provider_settings": json.dumps({"pace": 1.0}),
                "sort": sort, "now": now,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    for vid, *_rest in _ROSTER_VOICES:
        bind.execute(sa.text("DELETE FROM voice_profiles WHERE id = :id"), {"id": vid})
