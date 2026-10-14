// Insert only. Member updates are out of scope for this example. Because it
// fires on insert only, the subscriber's write-back never republishes.
trigger MemberRegisteredPublisher on Member__c (after insert) {
    List<Member_Registered__e> events = new List<Member_Registered__e>();
    for (Member__c m : Trigger.new) {
        // Keep the payload small. No other personal data.
        events.add(new Member_Registered__e(
            Member_Id__c       = m.Id,
            Email__c           = m.Email__c,
            Membership_Type__c = m.Membership_Type__c));
    }
    if (events.isEmpty()) {
        return;
    }
    // PublishAfterCommit => sent only when the transaction commits.
    // A failed publish doesn't throw, so check every SaveResult.
    List<Database.SaveResult> results = EventBus.publish(events);
    List<Relay_Error_Log__c> logs = new List<Relay_Error_Log__c>();
    for (Integer i = 0; i < results.size(); i++) {
        if (!results[i].isSuccess()) {
            logs.add(RelayErrorLog.entry(
                'MemberRegisteredPublisher',
                events[i].Member_Id__c,
                null,
                String.valueOf(results[i].getErrors())));
        }
    }
    // Don't block the user's save. The reconciliation job covers the gap.
    RelayErrorLog.write(logs);
}
