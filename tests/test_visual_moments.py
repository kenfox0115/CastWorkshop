import copy
import json
import unittest
from unittest.mock import Mock
from worker.conversation import respond
from worker.scene_variety import varied_scene, repair_target, STATE_FIELDS
from worker.visual_moments import choose_moment, moment_scene, canonical_posture


def moment(**changes):
    return dict(identity='Nyx, white hair and a mechanical left forearm', world_location='the same spaceship',
                room='galley', wardrobe='graphite flight suit', posture='seated', activity='drinking coffee',
                hands='holding a mug', gaze='toward the viewer', framing='medium', camera_angle='profile',
                intent='take a coffee break in the galley', constraints=[], repeat_requested=False, **changes)


def review(states, fields, changed=True):
    return json.dumps(dict(continuity_ok=True, repeat_requested=False, meaningful_change=changed,
        target_satisfied=True, reason='Concrete activity change', visual_state={key:'' for key in STATE_FIELDS},
        closest_recent_index=0 if states else -1,
        comparisons=[dict(recent_index=index, changed_fields=fields, meaningful_change=changed, reason='Compared to successful state')
                     for index in range(len(states))]))


class VisualMomentTests(unittest.TestCase):
    def test_shared_intention_precedes_dialogue_and_controls_compiled_scene(self):
        old=moment();old.update(room='cockpit', activity='checking navigation', hands='on controls', posture='standing')
        shared=moment()
        call=Mock(side_effect=[json.dumps(shared), json.dumps({"reply":"Let's take a coffee break in the galley."}),
            json.dumps(dict(response_type='image',visible_subjects=['character'],scene='Standing at the old cockpit console.',video_line=None)),
            review([old], ['room','posture','activity','hands'])])
        evict=Mock()
        result=respond(dict(message='I could use coffee. Send a picture.', recent_visual_states=[old],
                            recent_image_descriptions=['Old cockpit scene']), 'chosen', call, evict, mode='nsfw')
        self.assertEqual(result['response_type'],'image')
        self.assertIn('galley', result['scene'])
        self.assertIn('Posture: seated', result['scene'])
        self.assertNotIn('old cockpit console', result['scene'])
        self.assertIn('take a coffee break',call.call_args_list[1].kwargs['messages'][0]['content'])
        self.assertEqual(result['visual_state']['activity'],'drinking coffee')
        self.assertEqual(result['visual_state']['camera_angle'],'profile')
        evict.assert_called_once_with('chosen')

    def test_seated_leaning_modifier_does_not_select_another_seated_pose(self):
        self.assertEqual(canonical_posture({'description':'Nyx sits in a chair, leaning back.'}), 'seated')
        target=repair_target('Nyx sits in a chair, leaning back.', {'request':'another picture'}, 1)
        self.assertIn('standing',target)
        self.assertNotIn('character seated',target)

    def test_structured_state_prevents_fabricated_wording_novelty(self):
        shared=moment();states=[copy.deepcopy(shared)]
        context=dict(request='another picture', visual_moment=shared, recent_visual_states=states,
                     recent_image_descriptions=['A previous differently worded scene'])
        call=Mock(return_value=review(states,['posture','activity','hands','framing'],changed=True))
        _,state=varied_scene(moment_scene(shared),context,Mock(return_value=(moment_scene(shared),shared)),call,'chosen','2m')
        self.assertTrue(state['variety_exhausted'])
        self.assertEqual(state['variety_attempts'],3)

    def test_every_recent_successful_moment_must_be_compared(self):
        a=moment();b=moment();b['camera_angle']='front'
        broken=json.loads(review([a],['camera_angle']))
        context=dict(request='another picture',visual_moment=b,recent_visual_states=[a,a])
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            varied_scene(moment_scene(b),context,Mock(),Mock(return_value=json.dumps(broken)),'chosen','2m')

    def test_one_matching_recent_moment_prevents_acceptance(self):
        a=moment();b=moment();b.update(posture='standing',activity='preparing coffee')
        context=dict(request='another picture',visual_moment=b,recent_visual_states=[a,b])
        call=Mock(return_value=review([a,b],['posture','activity']))
        _,state=varied_scene(moment_scene(b),context,Mock(return_value=(moment_scene(b),b)),call,'chosen','2m')
        self.assertTrue(state['variety_exhausted'])

    def test_fixed_reply_and_recent_states_reach_repair_selector(self):
        shared=moment();call=Mock(return_value=json.dumps(shared))
        choose_moment(dict(message='Stay seated with coffee',recent_visual_states=[shared]),'chosen',call,'2m','nsfw',
                      previous=shared,repair='change camera only',reply='Still seated in the galley.')
        data=json.loads(call.call_args.kwargs['messages'][1]['content'])
        self.assertEqual(data['fixed_reply'],'Still seated in the galley.')
        self.assertEqual(data['previous_candidate'],shared)
        self.assertEqual(data['recent_successful_moments'][0]['posture'],'seated')

    def test_sfw_sanitization_preserves_json_and_does_not_mutate_state(self):
        shared=moment();payload=dict(message='Send a picture',system_prompt='Kind. Always horny.',recent_visual_states=[shared])
        original=copy.deepcopy(payload);call=Mock(return_value=json.dumps(shared))
        choose_moment(payload,'chosen',call,'2m','sfw')
        data=json.loads(call.call_args.kwargs['messages'][1]['content'])
        self.assertNotIn('horny',str(data))
        self.assertEqual(payload,original)

    def test_failed_shared_selector_preserves_conversation(self):
        call=Mock(side_effect=[TimeoutError(), 'Hello there.', json.dumps(dict(response_type='chat',visible_subjects=[],scene='',video_line=None))])
        result=respond(dict(message='Hello',recent_visual_states=[moment()]),'chosen',call,Mock(),mode='nsfw')
        self.assertEqual(result['text'],'Hello there.')
        self.assertEqual(result['response_type'],'chat')

    def test_allow_media_false_does_not_select_a_moment(self):
        call=Mock(return_value='Hello there.')
        respond(dict(message='Hello',recent_visual_states=[moment()],allow_media=False),'chosen',call,Mock(),mode='nsfw')
        self.assertEqual(call.call_count,1)

    def test_explicit_video_does_not_select_image_moment(self):
        call=Mock(side_effect=['Hello.',json.dumps(dict(response_type='video',visible_subjects=['character'],scene='The character waves.',video_line='Hello.'))])
        result=respond(dict(message='Send a video',recent_visual_states=[moment()]),'chosen',call,Mock(),mode='nsfw')
        self.assertEqual(result['response_type'],'video')
        self.assertEqual(call.call_count,2)

    def test_repair_cannot_change_reply_room_or_activity(self):
        previous=moment();wrong=moment();wrong.update(room="engine room",activity="repairing a pump")
        actual=choose_moment(dict(message="another picture",recent_visual_states=[previous]),'chosen',
                             Mock(return_value=json.dumps(wrong)),'2m','nsfw',previous=previous,reply="Coffee in the galley.")
        self.assertEqual(actual['room'],'galley')
        self.assertEqual(actual['activity'],'drinking coffee')

    def test_durable_repair_reuses_shared_moment_without_redrafting_reply(self):
        shared=moment();scene=moment_scene(shared)
        call=Mock(side_effect=[json.dumps(dict(response_type='image',visible_subjects=['character'],scene='Old cockpit scene.',video_line=None)),
                               review([dict(shared,room='cockpit')],['room'])])
        result=respond(dict(message='coffee picture',drafted_reply='Coffee in the galley.',shared_visual_moment=shared,
                            recent_visual_states=[dict(shared,room='cockpit')],recent_image_descriptions=['Old cockpit']),
                       'chosen',call,Mock(),mode='nsfw')
        self.assertEqual(result['response_type'],'image')
        self.assertIn('Location: galley',result['scene'])
        self.assertEqual(call.call_count,2)

    def test_missing_closest_diagnostic_does_not_discard_complete_comparisons(self):
        a=moment();b=moment();b['camera_angle']='front'
        data=json.loads(review([a],['camera_angle']));data['closest_recent_index']=-1
        _,state=varied_scene(moment_scene(b),dict(request='another picture',visual_moment=b,recent_visual_states=[a]),
                            Mock(),Mock(return_value=json.dumps(data)),'chosen','2m')
        self.assertFalse(state['variety_exhausted'])
        self.assertEqual(state['variety_reviews'][0]['closest_recent_index'],0)

    def test_world_location_does_not_freeze_legacy_room(self):
        from worker.visual_moments import compact_state
        state=compact_state(dict(world_location='interior cockpit of ship in deep space',room='cockpit'))
        self.assertEqual(state['world_location'],'ship in deep space')

    def test_boot_action_uses_frame_that_shows_boots(self):
        shared=moment();shared.update(activity='adjusting boot straps',hands='both hands on boots')
        actual=choose_moment(dict(message='Send a picture'), 'chosen', Mock(return_value=json.dumps(shared)), '2m','nsfw')
        self.assertEqual(actual['framing'],'full_body')

    def test_incomplete_review_is_retried_without_new_candidate(self):
        a=moment();b=moment();b['camera_angle']='front'
        incomplete=json.loads(review([a],['camera_angle']));incomplete['comparisons']=[]
        call=Mock(side_effect=[json.dumps(incomplete),review([a],['camera_angle'])]);replan=Mock()
        _,state=varied_scene(moment_scene(b),dict(request='another picture',visual_moment=b,recent_visual_states=[a]),
                            replan,call,'chosen','2m')
        self.assertEqual(state['variety_attempts'],1)
        self.assertEqual(call.call_count,2)
        replan.assert_not_called()
        schema=call.call_args.kwargs['json_mode']
        self.assertEqual(schema['properties']['comparisons']['minItems'],1)
        self.assertEqual(schema['properties']['comparisons']['maxItems'],1)

    def test_state_only_history_still_gets_structured_review(self):
        old=moment();old.update(room='cockpit',activity='checking navigation')
        shared=moment();call=Mock(side_effect=[json.dumps(shared),json.dumps({'reply':'Coffee in the galley.'}),
            json.dumps(dict(response_type='image',visible_subjects=['character'],scene='Old cockpit.',video_line=None)),
            review([old],['room','activity'])])
        result=respond(dict(message='Coffee picture',recent_visual_states=[old]),'chosen',call,Mock(),mode='nsfw')
        self.assertEqual(result['visual_state']['room'],'galley')
        self.assertEqual(call.call_count,4)

    def test_repeat_preserves_fixed_framing(self):
        shared=moment();shared.update(activity='adjusting boots',hands='holding boots',repeat_requested=True)
        actual=choose_moment(dict(message='Repeat the exact image'), 'chosen', Mock(return_value=json.dumps(shared)), '2m','nsfw')
        self.assertEqual(actual['framing'],'medium')

    def test_exact_state_repetition_is_replanned_before_reply(self):
        from worker.visual_moments import select_moment
        old=moment();new=moment();new.update(activity='brewing coffee',hands='operating coffee machine',posture='standing')
        call=Mock(side_effect=[json.dumps(old),json.dumps(new)])
        chosen,attempts,trace=select_moment(dict(message='Another picture',recent_visual_states=[old]),'chosen',call,'2m','nsfw')
        self.assertEqual(chosen['activity'],'brewing coffee')
        self.assertEqual(attempts,2)
        self.assertEqual(trace[0]['stage'],'moment_selection')
        self.assertEqual(call.call_count,2)

    def test_preselection_and_postselection_share_three_attempt_budget(self):
        from worker.visual_moments import select_moment
        old=moment();call=Mock(return_value=json.dumps(old))
        chosen,attempts,trace=select_moment(dict(message='Another picture',recent_visual_states=[old]),'chosen',call,'2m','nsfw')
        self.assertEqual(attempts,3)
        replan=Mock()
        _,state=varied_scene(moment_scene(chosen),dict(request='Another picture',visual_moment=chosen,recent_visual_states=[old]),
                            replan,Mock(return_value=review([old],[],changed=False)),'chosen','2m',
                            start_attempt=attempts,initial_trace=trace)
        self.assertEqual(state['variety_attempts'],3)
        self.assertTrue(state['variety_exhausted'])
        replan.assert_not_called()

    def test_full_repeat_bypasses_preselection_novelty(self):
        from worker.visual_moments import select_moment
        old=moment();old['repeat_requested']=True;call=Mock(return_value=json.dumps(old))
        _,attempts,_=select_moment(dict(message='Repeat the image',recent_visual_states=[old]),'chosen',call,'2m','nsfw',repeat_allowed=True)
        self.assertEqual(attempts,1)
        call.assert_not_called()

    def test_activity_repair_keeps_compatible_hands_and_gaze(self):
        old=moment();new=moment();new.update(activity='calibrating machinery',hands='holding a spanner',gaze='at machine')
        actual=choose_moment(dict(message='coffee picture'),'chosen',Mock(return_value=json.dumps(new)),'2m','nsfw',
                             previous=old,reply='Coffee in the galley.')
        self.assertEqual(actual['activity'],old['activity'])
        self.assertEqual(actual['hands'],old['hands'])
        self.assertEqual(actual['gaze'],old['gaze'])

    def test_shared_dialogue_establishes_room_and_removes_caption(self):
        from worker.visual_moments import shared_dialogue
        shared=moment();call=Mock(side_effect=[json.dumps({'reply':"Here's the cockpit. Coffee sounds good."}),
                                              json.dumps({'reply':"Let's get coffee in the galley. Here's a picture."})])
        reply=shared_dialogue([dict(role='system',content='Be natural.')],shared,[dict(room='cockpit')],
                             'Coffee picture','chosen',call,'2m')
        self.assertIn('galley',reply)
        self.assertNotIn('picture',reply)
        self.assertNotIn('cockpit',reply)

    def test_actual_compiled_scene_is_validated_not_unused_classifier_prose(self):
        from worker.conversation import read_plan
        bad=json.dumps(dict(response_type='image',visible_subjects=['character'],scene='The user sits beside the character.',video_line=None))
        actual=read_plan(Mock(return_value=bad),[],'chosen','2m',scene_override='The character sits alone.')
        self.assertEqual(actual['scene'],'The character sits alone.')
        with self.assertRaises(ValueError):
            read_plan(Mock(return_value=bad.replace('["character"]','["character", "other_person"]')),[],
                      'chosen','2m',attempts=1,scene_override='The character sits alone.')

    def test_full_repeat_copies_actual_successful_fields(self):
        from worker.visual_moments import select_moment
        old=moment();call=Mock(return_value='must not be used')
        actual,_,_=select_moment(dict(message='Repeat the last image exactly',recent_visual_states=[old]),
                                 'chosen',call,'2m','nsfw',repeat_allowed=True)
        for key in ('room','wardrobe','activity','hands','gaze','framing','camera_angle'):
            self.assertEqual(actual[key],old[key])
        self.assertTrue(actual['repeat_requested'])
        call.assert_not_called()

    def test_repeat_with_requested_change_does_not_restore_old_outfit(self):
        from worker.visual_moments import select_moment
        old=moment();new=moment();new.update(wardrobe='blue suit',repeat_requested=True)
        call=Mock(return_value=json.dumps(new))
        actual,_,_=select_moment(dict(message='Repeat that image but change into a blue suit',recent_visual_states=[old]),
                                 'chosen',call,'2m','nsfw',repeat_allowed=True)
        self.assertEqual(actual['wardrobe'],'blue suit')
        self.assertEqual(call.call_count,1)

    def test_viewer_gaze_does_not_turn_holding_mug_into_holding_user(self):
        from worker.conversation import normalize_scene_roles
        shared=moment();shared.update(hands='right hand holding a steaming mug',gaze='toward user')
        scene=normalize_scene_roles(moment_scene(shared))
        self.assertIn('holding a steaming mug',scene)
        self.assertIn('toward off-screen viewer',scene)

    def test_real_offscreen_physical_action_is_still_rejected(self):
        from worker.conversation import normalize_scene_roles
        shared=moment();shared.update(activity='the user holds the reference character',gaze='toward user')
        with self.assertRaises(ValueError):
            normalize_scene_roles(moment_scene(shared))

    def test_repeat_with_conflicting_posture_does_not_restore_old_pose(self):
        from worker.visual_moments import select_moment
        old=moment();old['posture']='standing';new=moment();new['repeat_requested']=True
        call=Mock(return_value=json.dumps(new))
        actual,_,_=select_moment(dict(message='Repeat the image in a seated pose',recent_visual_states=[old]),
                                 'chosen',call,'2m','nsfw',repeat_allowed=True)
        self.assertEqual(actual['posture'],'seated')
        self.assertEqual(call.call_count,1)

    def test_repeat_with_unmarked_new_outfit_uses_latest_request(self):
        from worker.visual_moments import select_moment
        old=moment();new=moment();new.update(wardrobe='blue suit',repeat_requested=True)
        call=Mock(return_value=json.dumps(new))
        actual,_,_=select_moment(dict(message='Repeat the image wearing a blue suit',recent_visual_states=[old]),
                                 'chosen',call,'2m','nsfw',repeat_allowed=True)
        self.assertEqual(actual['wardrobe'],'blue suit')
        self.assertEqual(call.call_count,1)

    def test_third_fallback_records_actual_wardrobe_not_unmet_target(self):
        shared=moment();shared['wardrobe']='old red coat';states=[dict(shared,wardrobe='blue suit')]
        data=json.loads(review(states,[],changed=False));data['target_satisfied']=False
        data['visual_state'].update(wardrobe='blue suit',action='drinking coffee',room='galley',posture='seated')
        scene='The reference character is seated in the galley wearing a blue suit, drinking coffee.'
        _,state=varied_scene(scene,dict(request='Wear the blue suit',visual_moment=shared,recent_visual_states=states),
                            Mock(),Mock(return_value=json.dumps(data)),'chosen','2m',start_attempt=3)
        self.assertEqual(state['wardrobe'],'blue suit')
        self.assertEqual(state['description'],scene)
        self.assertTrue(state['variety_exhausted'])

    def test_target_aliases_do_not_count_as_new_gaze(self):
        from worker.visual_moments import field_key
        self.assertEqual(field_key('gaze','toward the mug in their hand'),field_key('gaze','at the mug in their hand'))
        self.assertEqual(field_key('gaze','toward the user'),field_key('gaze','at the off-screen viewer'))
        self.assertNotEqual(field_key('gaze','at the viewer'),field_key('gaze','through the window'))

    def test_room_label_aliases_do_not_count_as_room_transition(self):
        from worker.visual_moments import field_key
        self.assertEqual(field_key('room','galley of the ship'),field_key('room',"the ship's galley"))
        self.assertNotEqual(field_key('room','galley'),field_key('room','engine room'))

    def test_synonymous_gaze_is_rejected_before_dialogue(self):
        from worker.visual_moments import select_moment
        old=moment();old['gaze']='at the mug in their hand'
        duplicate=moment();duplicate['gaze']='toward the mug in their hand'
        new=moment();new.update(gaze='through the viewport',activity='watching the stars')
        chosen,attempts,_=select_moment(dict(message='another picture',recent_visual_states=[old]),'chosen',
            Mock(side_effect=[json.dumps(duplicate),json.dumps(new)]),'2m','nsfw')
        self.assertEqual(attempts,2)
        self.assertEqual(chosen['gaze'],'through the viewport')

    def test_pairwise_review_cannot_credit_gaze_prepositions(self):
        old=moment();old['gaze']='at the mug in their hand'
        duplicate=moment();duplicate['gaze']='toward the mug in their hand'
        _,state=varied_scene(moment_scene(duplicate),dict(request='another picture',visual_moment=duplicate,recent_visual_states=[old]),
            Mock(return_value=(moment_scene(duplicate),duplicate)),Mock(return_value=review([old],['gaze','camera_angle'])),
            'chosen','2m')
        self.assertTrue(state['variety_exhausted'])
        self.assertEqual(state['variety_attempts'],3)
