@IsTest
private class MemberRelayTriggersTest {
    @IsTest
    static void publishesOnInsert() {
        Member__c member = new Member__c(
            Name               = 'Test Member',
            Email__c           = 'member@example.com',
            Membership_Type__c = 'Standard');
        Test.startTest();
        insert member;
        Test.stopTest();
        // The publish succeeded, so nothing was logged.
        Assert.areEqual(0, [SELECT COUNT() FROM Relay_Error_Log__c]);
        Assert.areEqual('Pending', [SELECT Sync_Status__c FROM Member__c WHERE Id = :member.Id].Sync_Status__c);
    }
    @IsTest
    static void subscriberWritesExternalIdAndLogsBadIds() {
        Member__c member = new Member__c(
            Name               = 'Synced Member',
            Email__c           = 'synced@example.com',
            Membership_Type__c = 'Premium');
        insert member;
        Test.startTest();
        EventBus.publish(new List<Member_Synced__e>{
            new Member_Synced__e(
                Member_Id__c          = member.Id,
                External_Member_Id__c = 'ext-1',
                Sync_Status__c        = 'Synced',
                Source_Event_Id__c    = 'evt-1'),
            new Member_Synced__e(
                Member_Id__c          = 'not-an-id',
                External_Member_Id__c = 'ext-2',
                Sync_Status__c        = 'Synced',
                Source_Event_Id__c    = 'evt-2')
        });
        Test.stopTest();
        Member__c synced = [
            SELECT External_Member_Id__c, Sync_Status__c FROM Member__c WHERE Id = :member.Id
        ];
        Assert.areEqual('ext-1', synced.External_Member_Id__c);
        Assert.areEqual('Synced', synced.Sync_Status__c);
        List<Relay_Error_Log__c> logs = [
            SELECT Source__c, Source_Event_Id__c FROM Relay_Error_Log__c
        ];
        Assert.areEqual(1, logs.size());
        Assert.areEqual('MemberSyncedSubscriber', logs[0].Source__c);
        Assert.areEqual('evt-2', logs[0].Source_Event_Id__c);
    }
}
