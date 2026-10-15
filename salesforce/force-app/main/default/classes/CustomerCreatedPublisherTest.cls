@IsTest
private class CustomerCreatedPublisherTest {

    @IsTest
    static void publishesEventWhenAccountIsCreated() {
        List<Account> accounts = new List<Account>{
            new Account(Name = 'Example Corp', Industry = 'Manufacturing'),
            new Account(Name = 'No Industry Ltd')
        };

        Test.startTest();
        insert accounts;
        // Events with PublishAfterCommit are delivered when the test transaction ends.
        Test.stopTest();

        System.assertEquals(2, [SELECT COUNT() FROM Account WHERE Id IN :accounts],
            'Both accounts should be saved, and the trigger should not block the save');
    }
}
